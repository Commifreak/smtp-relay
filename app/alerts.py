"""Alert mails for problems that need attention.

Alerts are sent as messages through the delivery queue (with retries, history and transcripts) to the
configured alert address. Each kind can be switched off; repeated events are throttled so a problem
produces one mail, and conditions with a state (relay down, queue backlog) also report the recovery.
"""
import json
import logging
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Settings
from .db import Database
from .rewrite import split_message

log = logging.getLogger("smtp-relay.alerts")

DAY = 86400
HOUR = 3600


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class Alerts:
    def __init__(self, db: Database, settings: Settings, queue):
        self.db = db
        self.settings = settings
        self.queue = queue

    # state

    def _state(self, key: str) -> dict:
        return self.db.query_one("SELECT active, last_sent FROM alert_state WHERE key = ?", (key,)) or \
            {"active": 0, "last_sent": None}

    def _set_state(self, key: str, active: bool, sent: bool = True) -> None:
        last_sent = time.time() if sent else self._state(key)["last_sent"]
        self.db.execute("INSERT OR REPLACE INTO alert_state (key, active, last_sent) VALUES (?, ?, ?)",
                        (key, int(active), last_sent))

    def _throttled(self, key: str, interval: float) -> bool:
        """True if an alert for `key` was sent within `interval`; otherwise records it as sent now."""
        last = self._state(key)["last_sent"]
        if last and time.time() - last < interval:
            return True
        self._set_state(key, False)
        return False

    # sending

    def _relay_for_alert(self, avoid: int | None) -> dict | None:
        relays = [r for r in self.queue.relays.all() if r["enabled"]]
        if not relays:
            return None
        preferred = self.settings.get("alert_relay_id")
        ordered = sorted(relays, key=lambda r: r["id"] != preferred)
        for relay in ordered:
            if relay["id"] != avoid:
                return relay
        return ordered[0]  # only the failing relay is left; the alert waits in the queue until it recovers

    def send(self, kind: str, subject: str, lines: list[str], avoid_relay: int | None = None,
             link: str | None = None, attachment: bytes | None = None) -> int | None:
        """Queue an alert mail. Returns the message id, or None if alerts of this kind are off."""
        s = self.settings.snapshot()
        to = s["alert_address"]
        if not to or (kind != "test" and not s.get(f"alert_{kind}", False)):
            return None
        relay = self._relay_for_alert(avoid_relay)
        if relay is None:
            log.warning("Alert not sent, no relay configured: %s", subject)
            return None
        sender = (relay["rewrite_from_address"] if relay["rewrite_enabled"] else "") or relay["username"] or to
        body = list(lines)
        if s["public_url"]:
            body += ["", f"Details: {s['public_url']}{link or '/'}"]
        body += ["", "--", "Sent by smtp-relay. Alerts can be configured under Settings → Alerts."]
        m = EmailMessage()
        m["From"] = f"smtp-relay <{sender}>"
        m["To"] = to
        m["Subject"] = f"[smtp-relay] {subject}"
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        m["Auto-Submitted"] = "auto-generated"
        m.set_content("\n".join(body))
        if attachment:
            m.add_attachment(attachment, maintype="text", subtype="rfc822-headers", filename="original-headers.txt")
        raw = m.as_bytes(policy=m.policy.clone(linesep="\r\n"))
        message_id = self.queue.accept(raw=raw, mail_from=sender, rcpts=[to], status="queued",
                                       client_ip="smtp-relay", client_name="Alert", is_notification=1,
                                       relay_id=relay["id"])
        self.queue.wake()
        log.info("Alert queued as message #%s: %s", message_id, subject)
        return message_id

    # events

    def test(self) -> int | None:
        return self.send("test", "Test alert", ["This is a test alert. Alert mails reach you this way."])

    def delivery_failed(self, row: dict, failed: list[str], attempts: int) -> None:
        client = row["client_ip"] or ""
        if row["client_name"]:
            client += f" ({row['client_name']})"
        attachment = b"".join(split_message(row["raw"])[0]) if row["raw"] else None
        self.send("delivery_failed", f"Undeliverable: {row['subject'] or '(no subject)'}", [
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
        ], link=f"/messages/{row['id']}", attachment=attachment)

    def relay_problem(self, relay: dict, error: str, message_id: int) -> None:
        key = f"relay:{relay['id']}"
        if self._state(key)["active"]:
            return
        self._set_state(key, True)
        self.send("relay_down", f"Relay “{relay['name']}” is not working", [
            f"smtp-relay could not deliver through the relay “{relay['name']}” ({relay['host']}:{relay['port']}).",
            "",
            f"Error: {error}",
            "",
            "Affected messages stay in the queue and are retried automatically. You will get another",
            "mail when the relay works again.",
        ], avoid_relay=relay["id"], link=f"/messages/{message_id}")

    def relay_ok(self, relay: dict) -> None:
        key = f"relay:{relay['id']}"
        if not self._state(key)["active"]:
            return
        self._set_state(key, False)
        self.send("relay_down", f"Relay “{relay['name']}” works again",
                  [f"Delivery through the relay “{relay['name']}” ({relay['host']}:{relay['port']}) works again."],
                  link="/relays")

    def check_queue_backlog(self) -> None:
        minutes = self.settings.get("alert_queue_backlog_minutes")
        row = self.db.query_one("SELECT COUNT(*) AS n, MIN(COALESCE(queued_at, received_at)) AS oldest "
                                "FROM messages WHERE status = 'queued' AND is_notification = 0")
        stuck = row["n"] and time.time() - row["oldest"] >= minutes * 60
        active = self._state("queue_backlog")["active"]
        if stuck and not active:
            self._set_state("queue_backlog", True)
            self.send("queue_backlog", f"{row['n']} message(s) waiting in the queue", [
                f"{row['n']} message(s) could not be delivered for more than {minutes} minutes.",
                f"The oldest one was received at {_fmt_time(row['oldest'])}.",
                "",
                "They are retried automatically. You will get another mail when the queue is empty again.",
            ], link="/queue")
        elif not row["n"] and active:
            self._set_state("queue_backlog", False)
            self.send("queue_backlog", "Queue is empty again", ["All queued messages have been processed."],
                      link="/queue")

    def sender_rejected(self, address: str, client_ip: str) -> None:
        if self._throttled(f"sender:{address.lower()}", DAY):
            return
        self.send("sender_rejected", f"Rejected unknown sender {address}", [
            f"A message from <{address}> (client {client_ip}) was rejected because no relay is configured",
            "for this sender. If this is one of your devices or services, add the address or its domain",
            "to the senders of a relay.",
            "",
            "Further rejections of this sender are not reported for 24 hours.",
        ], link="/relays")

    def client_rejected(self, client_ip: str) -> None:
        if self._throttled(f"client:{client_ip}", DAY):
            return
        self.send("client_rejected", f"Rejected connection from {client_ip}", [
            f"A connection from {client_ip} was refused because the address is not in the allowed client",
            "networks. If this is one of your devices, add its network under Settings.",
            "",
            "Further connections from this address are not reported for 24 hours.",
        ], link="/sessions")

    def login_failed(self, username: str, client_ip: str) -> None:
        if self._throttled(f"login:{client_ip}", HOUR):
            return
        self.send("login_failed", f"Failed login from {client_ip}", [
            f"Someone tried to log in to the web UI as “{username}” from {client_ip} with a wrong password.",
            "",
            "Further failed logins from this address are not reported for one hour.",
        ], link="/settings")
