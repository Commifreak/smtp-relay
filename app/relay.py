"""SMTP transport to the upstream relay (e.g. mailbox.org) with full protocol transcript."""
import asyncio
import smtplib
import ssl

from .db import Database, Transcript


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
                t.client(f"[message content: {len(s)} bytes]")
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
                return f"{parts[0]} {parts[1]} ******** (masked)"
            return line
        if self._auth_pending:
            return "******** (masked)"
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
        info = f"TLS active: {sock.version()}, cipher {cipher[0]}"
        if subject:
            info += f", certificate CN={subject.get('commonName')} (issuer: {issuer.get('organizationName') or issuer.get('commonName')})"
        return info
    except Exception:
        return "TLS active"


def _connect(settings: dict, t: Transcript):
    host, port, security = settings["relay_host"], settings["relay_port"], settings["relay_security"]
    ctx = ssl.create_default_context()
    if not settings.get("relay_tls_verify", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        t.info("Warning: TLS certificate verification is disabled")
    kwargs = {"local_hostname": settings["smtp_hostname"], "timeout": settings.get("relay_timeout") or 30}
    t.info(f"Connecting to {host}:{port} (mode: {security})")
    if security == "ssl":
        s = LoggingSMTP_SSL(context=ctx, **kwargs)
    else:
        s = LoggingSMTP(**kwargs)
    s.transcript = t
    s.connect(host, port)
    peer = s.sock.getpeername() if s.sock else None
    if peer:
        t.info(f"TCP connection established to {peer[0]}:{peer[1]}")
    if security == "ssl":
        t.info(_tls_info(s.sock))
    s.ehlo()
    if security == "starttls":
        s.starttls(context=ctx)
        t.info(_tls_info(s.sock))
        s.ehlo()
    if settings.get("relay_username"):
        s.login(settings["relay_username"], settings.get("relay_password", ""))
        t.info("Authentication successful")
    return s


def _quit(s, t: Transcript) -> None:
    try:
        s.quit()
    except Exception:
        s.close()
    t.info("Connection closed")


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


def reply_text(code: int, msg: bytes | str) -> str:
    if isinstance(msg, bytes):
        msg = msg.decode("utf-8", "replace")
    return f"{code} {' '.join(msg.split())}"


async def test_connection(db: Database, settings: dict) -> tuple[int, bool, str]:
    session_id = db.create_session("test", peer=f"{settings['relay_host']}:{settings['relay_port']}")
    t = Transcript(db, session_id)
    try:
        await asyncio.to_thread(test_connection_sync, settings, t)
        ok, detail = True, "Connection and login successful"
        t.info(detail)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
        t.error(detail)
    db.end_session(session_id, "ok" if ok else "error")
    return session_id, ok, detail
