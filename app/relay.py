"""Outbound delivery to the upstream relay (e.g. mailbox.org) with full protocol transcript."""
import asyncio
import json
import smtplib
import ssl
import time
from dataclasses import dataclass

from .db import Database, Transcript
from .rewrite import prepare


class _TranscriptMixin:
    """Hooks smtplib's send/getreply to record every protocol line (credentials masked)."""

    transcript: Transcript | None = None
    last_reply: tuple[int, bytes] | None = None
    _sending_data = False
    _auth_pending = False

    def send(self, s):
        t = self.transcript
        if t is not None:
            text = s.decode("utf-8", "replace") if isinstance(s, bytes) else s
            if self._sending_data and text.lower() != "data\r\n":
                t.client(f"[Nachrichteninhalt: {len(s)} Bytes]")
                t.client(".")
            else:
                for line in text.rstrip("\r\n").split("\r\n"):
                    t.client(self._mask(line))
        super().send(s)

    def _mask(self, line: str) -> str:
        parts = line.split(" ")
        if parts[0].upper() == "AUTH":
            self._auth_pending = True
            if len(parts) > 2:
                return f"{parts[0]} {parts[1]} ******** (maskiert)"
            return line
        if self._auth_pending:
            return "******** (maskiert)"
        return line

    def getreply(self):
        code, msg = super().getreply()
        if self.transcript is not None:
            lines = msg.split(b"\n") if msg else [b""]
            for i, line in enumerate(lines):
                sep = "-" if i < len(lines) - 1 else " "
                self.transcript.server(f"{code}{sep}{line.decode('utf-8', 'replace')}")
        if code != 334:
            self._auth_pending = False
        self.last_reply = (code, msg)
        return code, msg

    def data(self, msg):
        self._sending_data = True
        try:
            return super().data(msg)
        finally:
            self._sending_data = False


class LoggingSMTP(_TranscriptMixin, smtplib.SMTP):
    pass


class LoggingSMTP_SSL(_TranscriptMixin, smtplib.SMTP_SSL):
    pass


def _tls_info(sock) -> str:
    try:
        cipher = sock.cipher()
        cert = sock.getpeercert() or {}
        subject = dict(x[0] for x in cert.get("subject", ()))
        issuer = dict(x[0] for x in cert.get("issuer", ()))
        info = f"TLS aktiv: {sock.version()}, Cipher {cipher[0]}"
        if subject:
            info += f", Zertifikat CN={subject.get('commonName')} (Aussteller: {issuer.get('organizationName') or issuer.get('commonName')})"
        return info
    except Exception:
        return "TLS aktiv"


def _connect(settings: dict, t: Transcript):
    host, port, security = settings["relay_host"], settings["relay_port"], settings["relay_security"]
    ctx = ssl.create_default_context()
    if not settings.get("relay_tls_verify", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        t.info("Achtung: TLS-Zertifikatsprüfung ist deaktiviert")
    kwargs = {"local_hostname": settings["smtp_hostname"], "timeout": settings.get("relay_timeout") or 30}
    t.info(f"Verbinde zu {host}:{port} (Modus: {security})")
    if security == "ssl":
        s = LoggingSMTP_SSL(context=ctx, **kwargs)
    else:
        s = LoggingSMTP(**kwargs)
    s.transcript = t
    s.connect(host, port)
    peer = s.sock.getpeername() if s.sock else None
    if peer:
        t.info(f"TCP-Verbindung hergestellt zu {peer[0]}:{peer[1]}")
    if security == "ssl":
        t.info(_tls_info(s.sock))
    s.ehlo()
    if security == "starttls":
        s.starttls(context=ctx)
        t.info(_tls_info(s.sock))
        s.ehlo()
    if settings.get("relay_username"):
        s.login(settings["relay_username"], settings.get("relay_password", ""))
        t.info("Authentifizierung erfolgreich")
    return s


def _quit(s, t: Transcript) -> None:
    try:
        s.quit()
    except Exception:
        s.close()
    t.info("Verbindung geschlossen")


def send_sync(settings: dict, envelope_from: str, rcpts: list[str], data: bytes,
              t: Transcript, smtputf8: bool = False) -> tuple[dict, tuple[int, bytes] | None]:
    s = _connect(settings, t)
    try:
        options = []
        if smtputf8 and s.has_extn("smtputf8"):
            options.append("SMTPUTF8")
        if any(b > 127 for b in data) and s.has_extn("8bitmime"):
            options.append("BODY=8BITMIME")
        refused = s.sendmail(envelope_from, rcpts, data, mail_options=options)
        reply = s.last_reply
    finally:
        _quit(s, t)
    return refused, reply


def test_connection_sync(settings: dict, t: Transcript) -> None:
    s = _connect(settings, t)
    try:
        s.noop()
    finally:
        _quit(s, t)


@dataclass
class DeliveryResult:
    status: str        # relayed | partial | deferred | failed
    code: int
    detail: str

    def smtp_response(self) -> str:
        text = _ascii(self.detail)[:200]
        if self.status in ("relayed", "partial"):
            return f"250 2.0.0 Ok: relayed ({text})"
        if self.status == "deferred":
            return f"451 4.4.1 Relay temporarily failed: {text}"
        code = self.code if 500 <= self.code < 600 else 554
        return f"{code} 5.0.0 Relay rejected: {text}"


def _ascii(text: str) -> str:
    return " ".join(text.encode("ascii", "replace").decode().split())


def _reply_text(code: int, msg: bytes | str) -> str:
    if isinstance(msg, bytes):
        msg = msg.decode("utf-8", "replace")
    return f"{code} {' '.join(msg.split())}"


async def deliver(db: Database, settings: dict, message_id: int, raw: bytes, mail_from: str,
                  rcpts: list[str], trace: dict | None = None, smtputf8: bool = False) -> DeliveryResult:
    data, envelope_from, notes = prepare(raw, mail_from, settings, trace)
    session_id = db.create_session("outbound", peer=f"{settings['relay_host']}:{settings['relay_port']}",
                                   message_id=message_id)
    t = Transcript(db, session_id)
    for note in notes:
        t.info(note)
    db.update_message(message_id, status="relaying", relay_from=envelope_from)
    started = time.time()
    try:
        refused, reply = await asyncio.to_thread(send_sync, settings, envelope_from, rcpts, data, t, smtputf8)
        upstream = _reply_text(*reply) if reply else "ok"
        if refused:
            rej = ", ".join(f"{r}: {_reply_text(*v)}" for r, v in refused.items())
            result = DeliveryResult("partial", 250, f"{upstream}; abgelehnt: {rej}")
        else:
            result = DeliveryResult("relayed", 250, upstream)
    except smtplib.SMTPRecipientsRefused as e:
        codes = [c for c, _ in e.recipients.values()]
        rej = "; ".join(f"{r}: {_reply_text(*v)}" for r, v in e.recipients.items())
        temporary = all(400 <= c < 500 for c in codes)
        result = DeliveryResult("deferred" if temporary else "failed", max(codes or [550]),
                                f"Alle Empfänger abgelehnt: {rej}")
    except smtplib.SMTPResponseException as e:
        temporary = 400 <= e.smtp_code < 500
        result = DeliveryResult("deferred" if temporary else "failed", e.smtp_code,
                                f"{type(e).__name__}: {_reply_text(e.smtp_code, e.smtp_error)}")
    except (smtplib.SMTPException, OSError) as e:
        result = DeliveryResult("deferred", 451, f"{type(e).__name__}: {e}")
    if result.status in ("deferred", "failed"):
        t.error(result.detail)
    else:
        t.info(f"Zustellung an Relay erfolgreich ({time.time() - started:.2f}s)")
    db.end_session(session_id, result.status)
    db.update_message(message_id, status=result.status, detail=result.detail,
                      relayed_at=time.time() if result.status in ("relayed", "partial") else None)
    return result


async def resend(db: Database, settings: dict, message_id: int) -> DeliveryResult | None:
    row = db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
    if not row or not row["raw"]:
        return None
    trace = {"helo": row["helo"], "client_name": row["client_name"], "client_ip": row["client_ip"],
             "id": message_id, "rcpt": json.loads(row["rcpt_to"] or "[]")[0] if row["rcpt_to"] else ""}
    return await deliver(db, settings, message_id, row["raw"], row["mail_from"],
                         json.loads(row["rcpt_to"] or "[]"), trace)


async def test_connection(db: Database, settings: dict) -> tuple[int, bool, str]:
    session_id = db.create_session("test", peer=f"{settings['relay_host']}:{settings['relay_port']}")
    t = Transcript(db, session_id)
    try:
        await asyncio.to_thread(test_connection_sync, settings, t)
        ok, detail = True, "Verbindung und Anmeldung erfolgreich"
        t.info(detail)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
        t.error(detail)
    db.end_session(session_id, "ok" if ok else "fehler")
    return session_id, ok, detail
