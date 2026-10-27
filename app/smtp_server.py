"""Inbound SMTP server for the local network with full protocol transcript."""
import asyncio
import json
import logging
import socket

from aiosmtpd.smtp import SMTP

from .config import Settings, address_matches, ip_allowed, normalize_ip
from .db import Database, Transcript
from .relay import deliver
from .rewrite import header_meta

log = logging.getLogger("mta-relay.smtp")


class LoggingSMTP(SMTP):
    """aiosmtpd SMTP protocol that records every line exchanged with the client."""

    def __init__(self, handler, db: Database, settings: Settings, **kwargs):
        super().__init__(handler, **kwargs)
        self.db = db
        self.settings = settings
        self.transcript: Transcript | None = None
        self.db_session_id: int | None = None
        self.client_ip = ""
        self.client_name: str | None = None
        self._rxbuf = b""
        self._in_data = False
        self._data_bytes = 0
        self._data_lines = 0
        self._auth_pending = False

    def connection_made(self, transport):
        if self.transcript is None:
            peer = transport.get_extra_info("peername") or ("?", 0)
            self.client_ip = normalize_ip(peer[0])
            self.db_session_id = self.db.create_session("inbound", peer=f"{self.client_ip}:{peer[1]}")
            self.transcript = Transcript(self.db, self.db_session_id)
            self.transcript.info(f"Verbindung von {self.client_ip}:{peer[1]}")
            super().connection_made(transport)
            self.loop.create_task(self._resolve_peer())
        else:
            # Second call after STARTTLS
            super().connection_made(transport)
            ssl_obj = transport.get_extra_info("ssl_object")
            if ssl_obj is not None:
                self.transcript.info(f"STARTTLS ausgehandelt: {ssl_obj.version()}, Cipher {ssl_obj.cipher()[0]}")

    async def _resolve_peer(self):
        try:
            name, _ = await asyncio.wait_for(
                self.loop.getnameinfo((self.client_ip, 0), socket.NI_NAMEREQD), 3)
            self.client_name = name
            self.db.update_session(self.db_session_id, peer_name=name)
            self.transcript.info(f"Reverse DNS: {self.client_ip} → {name}")
        except Exception:
            self.transcript.info(f"Reverse DNS: kein PTR-Eintrag für {self.client_ip}")

    def connection_lost(self, exc):
        if self.transcript is not None:
            if self._rxbuf.strip():
                self.transcript.client(self._rxbuf.decode("utf-8", "replace") + " [unvollständig]")
            self.transcript.info("Verbindung geschlossen" + (f": {exc}" if exc else ""))
            self.db.end_session(self.db_session_id)
        super().connection_lost(exc)

    def data_received(self, data: bytes):
        self._record_incoming(data)
        super().data_received(data)

    def _record_incoming(self, data: bytes):
        t = self.transcript
        self._rxbuf += data
        while b"\n" in self._rxbuf:
            line, self._rxbuf = self._rxbuf.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if self._in_data:
                if line == b".":
                    t.client(f"[Nachrichteninhalt: {self._data_bytes} Bytes, {self._data_lines} Zeilen]")
                    t.client(".")
                    self._in_data = False
                else:
                    self._data_bytes += len(line) + 2
                    self._data_lines += 1
            else:
                t.client(self._mask(line.decode("utf-8", "replace")))
        if len(self._rxbuf) > 8192:
            if self._in_data:
                self._data_bytes += len(self._rxbuf)
            else:
                t.client(self._rxbuf[:200].decode("utf-8", "replace") + f" … [Zeile zu lang, {len(self._rxbuf)} Bytes]")
            self._rxbuf = b""

    def _mask(self, line: str) -> str:
        parts = line.split(" ")
        if parts[0].upper() == "AUTH":
            self._auth_pending = True
            return f"{parts[0]} {parts[1]} ******** (maskiert)" if len(parts) > 2 else line
        if self._auth_pending:
            return "******** (maskiert)"
        return line

    async def push(self, status):
        text = status.decode("utf-8", "replace") if isinstance(status, bytes) else status
        if self.transcript is not None:
            self.transcript.server(text)
        if text.startswith("354"):
            self._in_data = True
            self._data_bytes = self._data_lines = 0
        if not text.startswith("334"):
            self._auth_pending = False
        await super().push(status)

    async def _handle_client(self):
        networks = self.settings.get("allowed_networks")
        if not ip_allowed(self.client_ip, networks):
            self.transcript.error(f"Client-IP {self.client_ip} ist nicht in den erlaubten Netzen")
            self.db.update_session(self.db_session_id, result="abgelehnt: Netz")
            await self.push(f"554 5.7.1 Client {self.client_ip} not allowed to relay")
            self.transport.close()
            return
        await super()._handle_client()


class RelayHandler:
    """aiosmtpd handler enforcing sender/recipient policy and relaying synchronously."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    async def handle_EHLO(self, server, session, envelope, hostname, responses):
        session.host_name = hostname
        server.db.update_session(server.db_session_id, helo=hostname)
        return responses

    async def handle_HELO(self, server, session, envelope, hostname):
        session.host_name = hostname
        server.db.update_session(server.db_session_id, helo=hostname)
        return f"250 {server.hostname}"

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        allowed = self.settings.get("allowed_senders")
        if not address_matches(address, allowed):
            reason = "Absenderliste ist leer" if not allowed else "nicht in der Absenderliste"
            server.transcript.error(f"Absender <{address}> abgelehnt ({reason})")
            self.db.insert_message(session_id=server.db_session_id, client_ip=server.client_ip,
                                   client_name=server.client_name, helo=session.host_name,
                                   mail_from=address, rcpt_to="[]", status="rejected",
                                   detail=f"Absender nicht erlaubt ({reason})")
            self.db.update_session(server.db_session_id, result="abgelehnt: Absender")
            return f"550 5.7.1 Sender address <{address}> not allowed"
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 2.1.0 Ok"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        allowed = self.settings.get("allowed_recipients")
        if allowed and not address_matches(address, allowed):
            server.transcript.error(f"Empfänger <{address}> abgelehnt (nicht in der Empfängerliste)")
            self.db.update_session(server.db_session_id, result="abgelehnt: Empfänger")
            return f"550 5.7.1 Recipient address <{address}> not allowed"
        envelope.rcpt_tos.append(address)
        envelope.rcpt_options.extend(rcpt_options)
        return "250 2.1.5 Ok"

    async def handle_DATA(self, server, session, envelope):
        raw = envelope.original_content or envelope.content
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        settings = self.settings.snapshot()
        meta = header_meta(raw)
        message_id = self.db.insert_message(
            session_id=server.db_session_id, client_ip=server.client_ip, client_name=server.client_name,
            helo=session.host_name, mail_from=envelope.mail_from, rcpt_to=json.dumps(envelope.rcpt_tos),
            size=len(raw), status="received", raw=raw if settings["store_content"] else None, **meta)
        server.transcript.info(f"Nachricht #{message_id} angenommen ({len(raw)} Bytes), leite an "
                               f"{settings['relay_host']} weiter …")
        trace = {"helo": session.host_name, "client_name": server.client_name, "client_ip": server.client_ip,
                 "id": message_id, "rcpt": envelope.rcpt_tos[0]}
        result = await deliver(self.db, settings, message_id, raw, envelope.mail_from, envelope.rcpt_tos,
                               trace, smtputf8=envelope.smtp_utf8)
        (server.transcript.info if result.status in ("relayed", "partial") else server.transcript.error)(
            f"Nachricht #{message_id}: {result.status} – {result.detail}")
        self.db.update_session(server.db_session_id, result=result.status)
        return result.smtp_response()


def protocol_factory(db: Database, settings: Settings, tls_context=None):
    handler = RelayHandler(db, settings)

    def factory():
        size = settings.get("max_message_size_mb")
        return LoggingSMTP(handler, db, settings,
                           hostname=settings.get("smtp_hostname"),
                           ident="MTA-Relay",
                           data_size_limit=size * 1024 * 1024 if size else None,
                           enable_SMTPUTF8=True,
                           tls_context=tls_context,
                           timeout=300)
    return factory
