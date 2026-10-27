"""Store-and-forward delivery: every message is stored first, then delivered with retries.

Per recipient a delivery attempt ends in one of three ways: accepted by the relay,
rejected permanently (5xx) or failed temporarily (4xx / connection problems).
Temporarily failed recipients stay in the queue until they succeed or the maximum
queue lifetime is exceeded.
"""
import asyncio
import json
import logging
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Settings, parse_intervals
from .db import Database, Transcript
from .relay import reply_text, send_sync
from .rewrite import header_meta, prepare, split_message

log = logging.getLogger("smtp-relay.queue")

FINAL = ("relayed", "partial", "failed")
BATCH = 20


ASCII_REPLACEMENTS = str.maketrans({"–": "-", "—": "-", "→": "->", "…": "...", "„": '"', "“": '"', "”": '"'})


def _ascii(text: str) -> str:
    """SMTP replies must be ASCII: map common typography, replace anything else."""
    text = text.translate(ASCII_REPLACEMENTS)
    return " ".join(text.encode("ascii", "replace").decode().split())


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class AttemptResult:
    status: str        # relayed | partial | queued | failed
    detail: str
    code: int = 250    # upstream code, used for permanent failures

    def smtp_response(self, message_id: int) -> str:
        text = _ascii(self.detail)[:200]
        if self.status in ("relayed", "partial"):
            return f"250 2.0.0 Ok: relayed as #{message_id} ({text})"
        if self.status == "queued":
            return f"250 2.0.0 Ok: queued as #{message_id}, will retry ({text})"
        code = self.code if 500 <= self.code < 600 else 554
        return f"{code} 5.0.0 Relay rejected: {text}"


class DeliveryQueue:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()

    # entry points

    def accept(self, *, raw: bytes, mail_from: str, rcpts: list[str], status: str = "relaying",
               **fields) -> int:
        """Store a new message for delivery and return its id."""
        now = time.time()
        return self.db.insert_message(
            received_at=now, queued_at=now, mail_from=mail_from, rcpt_to=json.dumps(rcpts),
            pending_rcpts=json.dumps(rcpts), delivered_rcpts="[]", failed_rcpts="[]",
            size=len(raw), raw=raw, status=status,
            next_attempt_at=now if status == "queued" else None,
            **header_meta(raw), **fields)

    async def deliver_now(self, message_id: int) -> AttemptResult | None:
        row = self.db.query_one("SELECT status FROM messages WHERE id = ?", (message_id,))
        if not row or row["status"] != "queued":
            return None
        return await self.attempt(message_id)

    async def resend(self, message_id: int) -> AttemptResult | None:
        """Deliver a finished message again to all of its recipients."""
        row = self.db.query_one("SELECT status, raw, rcpt_to FROM messages WHERE id = ?", (message_id,))
        if not row or not row["raw"] or row["status"] not in FINAL:
            return None
        self.db.update_message(message_id, pending_rcpts=row["rcpt_to"], delivered_rcpts="[]",
                               failed_rcpts="[]", attempts=0, queued_at=time.time())
        return await self.attempt(message_id)

    def remove(self, message_id: int) -> bool:
        row = self.db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
        if not row or row["status"] != "queued":
            return False
        reason = "Manually removed from the queue"
        failed = json.loads(row["failed_rcpts"] or "[]") + \
            [f"{r}: {reason}" for r in json.loads(row["pending_rcpts"] or "[]")]
        fields = {"status": "partial" if json.loads(row["delivered_rcpts"] or "[]") else "failed",
                  "detail": reason, "pending_rcpts": "[]", "failed_rcpts": json.dumps(failed),
                  "next_attempt_at": None}
        if not self.settings.get("store_content"):
            fields["raw"] = None
        self.db.update_message(message_id, **fields)
        return True

    def wake(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._wake.set)

    def queued_count(self) -> int:
        return self.db.query_one("SELECT COUNT(*) AS n FROM messages WHERE status = 'queued'")["n"]

    # worker

    def recover(self) -> None:
        """Requeue messages whose delivery was interrupted (e.g. by a container restart)."""
        cur = self.db.execute(
            "UPDATE messages SET status = 'queued', next_attempt_at = ? "
            "WHERE status IN ('relaying', 'received') AND raw IS NOT NULL AND pending_rcpts IS NOT NULL",
            (time.time(),))
        if cur.rowcount:
            log.info("Requeued %d interrupted delivery attempt(s)", cur.rowcount)

    async def process_due(self) -> int:
        done = 0
        while not self.settings.get("queue_paused"):
            row = self.db.query_one(
                "SELECT id FROM messages WHERE status = 'queued' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at LIMIT 1", (time.time(),))
            if not row or done >= BATCH:
                break
            await self.attempt(row["id"])
            done += 1
        return done

    def _seconds_until_next(self) -> float:
        if self.settings.get("queue_paused"):
            return 60
        row = self.db.query_one("SELECT MIN(next_attempt_at) AS t FROM messages WHERE status = 'queued'")
        if not row or row["t"] is None:
            return 60
        return min(max(row["t"] - time.time(), 0.5), 60)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.recover()
        while True:
            self._wake.clear()
            try:
                await self.process_due()
            except Exception:
                log.exception("Error while processing the queue")
            try:
                await asyncio.wait_for(self._wake.wait(), self._seconds_until_next())
            except TimeoutError:
                pass

    # delivery

    async def attempt(self, message_id: int) -> AttemptResult:
        row = self.db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
        settings = self.settings.snapshot()
        pending = json.loads(row["pending_rcpts"] or "[]")
        delivered = json.loads(row["delivered_rcpts"] or "[]")
        failed = json.loads(row["failed_rcpts"] or "[]")
        attempt_no = row["attempts"] + 1
        now = time.time()
        self.db.update_message(message_id, status="relaying", attempts=attempt_no, last_attempt_at=now)

        trace = None
        if row["session_id"]:
            trace = {"helo": row["helo"], "client_name": row["client_name"], "client_ip": row["client_ip"],
                     "id": message_id, "rcpt": pending[0] if pending else ""}
        data, envelope_from, notes = prepare(row["raw"], row["mail_from"], settings, trace)
        session_id = self.db.create_session(
            "outbound", peer=f"{settings['relay_host']}:{settings['relay_port']}", message_id=message_id)
        t = Transcript(self.db, session_id)
        t.info(f"Delivery attempt {attempt_no} for {len(pending)} recipient(s): {', '.join(pending)}")
        for note in notes:
            t.info(note)
        self.db.update_message(message_id, relay_from=envelope_from)

        smtputf8 = not all(a.isascii() for a in [row["mail_from"], *pending])
        temp: dict[str, tuple[int, str]] = {}
        perm: dict[str, tuple[int, str]] = {}
        upstream = ""
        try:
            refused, reply = await asyncio.to_thread(send_sync, settings, envelope_from, pending, data, t, smtputf8)
            upstream = reply_text(*reply) if reply else "250 Ok"
            for rcpt, (code, msg) in refused.items():
                (temp if 400 <= code < 500 else perm)[rcpt] = (code, reply_text(code, msg))
        except smtplib.SMTPRecipientsRefused as e:
            upstream = "All recipients refused"
            for rcpt, (code, msg) in e.recipients.items():
                (temp if 400 <= code < 500 else perm)[rcpt] = (code, reply_text(code, msg))
        except smtplib.SMTPResponseException as e:
            upstream = f"{type(e).__name__}: {reply_text(e.smtp_code, e.smtp_error)}"
            target = temp if 400 <= e.smtp_code < 500 else perm
            for rcpt in pending:
                target[rcpt] = (e.smtp_code, upstream)
        except Exception as e:  # connection problems and anything unexpected: retry later
            upstream = f"{type(e).__name__}: {e}"
            for rcpt in pending:
                temp[rcpt] = (451, upstream)

        accepted = [r for r in pending if r not in temp and r not in perm]
        if accepted:
            t.info(f"Accepted by relay for: {', '.join(accepted)}")
        for rcpt, (_, why) in perm.items():
            t.error(f"Permanently rejected: {rcpt} – {why}")
        delivered += accepted
        failed += [f"{r}: {why}" for r, (_, why) in perm.items()]
        pending = list(temp)

        next_at = None
        status = None
        if pending:
            first_error = next(iter(temp.values()))[1]
            age = now - (row["queued_at"] or row["received_at"])
            if age < settings["queue_max_age_hours"] * 3600:
                intervals = parse_intervals(settings["retry_intervals"])
                delay = intervals[min(attempt_no - 1, len(intervals) - 1)]
                next_at = now + delay * 60
                status = "queued"
                detail = f"Temporary failure: {first_error} – next attempt {_fmt_time(next_at)}"
                t.error(f"Temporary failure for {', '.join(pending)} – next attempt in {delay} min "
                        f"({_fmt_time(next_at)})")
            else:
                reason = f"Maximum queue lifetime of {settings['queue_max_age_hours']} h exceeded"
                t.error(f"{reason} – giving up")
                failed += [f"{r}: {reason} (last error: {temp[r][1]})" for r in pending]
                pending = []
        if status is None:
            if delivered and failed:
                status, detail = "partial", f"{upstream}; failed: {'; '.join(failed)}"
            elif delivered:
                status, detail = "relayed", upstream
            else:
                status, detail = "failed", "; ".join(failed)

        self.db.end_session(session_id, status)
        fields = {"status": status, "detail": detail, "next_attempt_at": next_at,
                  "pending_rcpts": json.dumps(pending), "delivered_rcpts": json.dumps(delivered),
                  "failed_rcpts": json.dumps(failed)}
        if status in ("relayed", "partial"):
            fields["relayed_at"] = now
        if status in FINAL and not settings["store_content"]:
            fields["raw"] = None
        self.db.update_message(message_id, **fields)

        if status in ("partial", "failed") and not row["is_notification"]:
            self._notify(row, settings, failed, attempt_no)
        codes = [c for c, _ in perm.values()]
        return AttemptResult(status, detail, max(codes) if codes else 554)

    def _notify(self, row: dict, settings: dict, failed: list[str], attempts: int) -> None:
        to = settings.get("notify_address")
        if not to:
            return
        sender = settings.get("rewrite_from_address") or settings.get("relay_username") or to
        m = EmailMessage()
        m["From"] = f"smtp-relay <{sender}>"
        m["To"] = to
        m["Subject"] = f"Undeliverable: {row['subject'] or '(no subject)'}"
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        m["Auto-Submitted"] = "auto-generated"
        client = row["client_ip"] or ""
        if row["client_name"]:
            client += f" ({row['client_name']})"
        lines = [
            "smtp-relay could not (fully) deliver the following message.",
            "",
            f"Message:     #{row['id']}",
            f"Received:    {_fmt_time(row['received_at'])}",
            f"Client:      {client}",
            f"Sender:      {row['mail_from']}",
            f"Recipients:  {', '.join(json.loads(row['rcpt_to'] or '[]'))}",
            f"Subject:     {row['subject'] or ''}",
            f"Attempts:    {attempts}",
            "",
            "Failed recipients:",
            *[f"  - {f}" for f in failed],
            "",
            "The full SMTP transcripts are available in the web UI under the message number.",
        ]
        m.set_content("\n".join(lines))
        if row["raw"]:
            fields, _ = split_message(row["raw"])
            m.add_attachment(b"".join(fields), maintype="text", subtype="rfc822-headers",
                             filename="original-headers.txt")
        raw = m.as_bytes(policy=m.policy.clone(linesep="\r\n"))
        self.accept(raw=raw, mail_from=sender, rcpts=[to], status="queued", client_ip="smtp-relay",
                    client_name="Notification", is_notification=1)
        self.wake()
