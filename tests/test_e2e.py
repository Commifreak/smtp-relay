"""End-to-end: client -> smtp-relay (incl. delivery queue) -> fake upstream relay."""
import asyncio
import json
import smtplib
import socket
import threading
import time

import pytest
from aiosmtpd.controller import Controller
from fastapi.testclient import TestClient

from app.auth import set_credentials
from app.config import Settings
from app.db import Database
from app.delivery import DeliveryQueue
from app.smtp_server import protocol_factory
from app.web import create_app


class Upstream:
    def __init__(self):
        self.messages = []
        self.reject_rcpt = set()
        self.temp_rcpt = set()

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if address in self.reject_rcpt:
            return "550 5.1.1 No such user"
        if address in self.temp_rcpt:
            return "450 4.2.0 Mailbox busy, try later"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope)
        return "250 2.0.0 Ok: queued as ABC123"


class Env:
    def __init__(self, db, settings, queue, upstream, port, up_port, loop):
        self.db, self.settings, self.queue, self.upstream = db, settings, queue, upstream
        self.port, self.up_port, self.loop = port, up_port, loop

    def run(self, coro):
        """Run a coroutine on the server's event loop."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)

    def send(self, mail_from="nas@intern.lan", rcpts=("me@example.de",), msg=None):
        with smtplib.SMTP("127.0.0.1", self.port) as c:
            c.ehlo("nas.home.lan")
            return c.sendmail(mail_from, list(rcpts), msg or MSG)

    @property
    def relay_id(self):
        return self.queue.relays.first()["id"]

    def relay_down(self):
        assert self.queue.relays.save({"port": 1}, self.relay_id)[1] == []

    def relay_up(self):
        assert self.queue.relays.save({"port": self.up_port}, self.relay_id)[1] == []

    def make_due(self):
        self.db.execute("UPDATE messages SET next_attempt_at = 0 WHERE status = 'queued'")

    def message(self, message_id=1):
        return self.db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))


@pytest.fixture
def env(tmp_path):
    upstream = Upstream()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        up_port = sock.getsockname()[1]
    ctrl = Controller(upstream, hostname="127.0.0.1", port=up_port)
    ctrl.start()
    db = Database(str(tmp_path / "t.db"))
    settings = Settings(db)
    assert not settings.update({"relay_timeout": 5, "allowed_networks": ["127.0.0.0/8"]})
    queue = DeliveryQueue(db, settings)
    _, errors = queue.relays.save({"name": "upstream", "host": "127.0.0.1", "port": up_port, "security": "none",
                                   "senders": ["nas@intern.lan", "@home.lan"], "rewrite_enabled": True,
                                   "rewrite_from_address": "relay@example.de"})
    assert not errors
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    server = asyncio.run_coroutine_threadsafe(
        loop.create_server(protocol_factory(db, settings, queue), "127.0.0.1", 0), loop).result()
    port = server.sockets[0].getsockname()[1]
    yield Env(db, settings, queue, upstream, port, up_port, loop)
    loop.call_soon_threadsafe(server.close)
    loop.call_soon_threadsafe(loop.stop)
    ctrl.stop()


def web_client(env) -> TestClient:
    """Web UI client with completed setup, logged in as the local admin."""
    set_credentials(env.settings, "admin", "secret-password")
    env.settings.set_internal(setup_completed=True)
    client = TestClient(create_app(env.db, env.settings, env.queue))
    r = client.post("/login", data={"username": "admin", "password": "secret-password"}, follow_redirects=False)
    assert r.status_code == 303
    return client


MSG = "From: NAS <nas@intern.lan>\r\nTo: me@example.de\r\nSubject: Backup ok\r\n\r\nAll good.\r\n"


def test_relay_success(env):
    env.send()
    assert len(env.upstream.messages) == 1
    env_up = env.upstream.messages[0]
    assert env_up.mail_from == "relay@example.de"
    assert b"From: NAS <relay@example.de>" in env_up.content
    assert b"X-Original-From: NAS <nas@intern.lan>" in env_up.content
    m = env.message()
    assert m["status"] == "relayed"
    assert m["subject"] == "Backup ok"
    assert m["attempts"] == 1
    assert json.loads(m["delivered_rcpts"]) == ["me@example.de"]
    assert json.loads(m["pending_rcpts"]) == []
    assert "queued as ABC123" in m["detail"]
    sessions = {s["kind"]: s for s in env.db.query("SELECT * FROM sessions")}
    inbound = [l["line"] for l in env.db.transcript(sessions["inbound"]["id"])]
    outbound = [l["line"] for l in env.db.transcript(sessions["outbound"]["id"])]
    assert any(l.lower().startswith("ehlo nas.home.lan") for l in inbound)
    assert any(l.lower().startswith("mail from:<nas@intern.lan>") for l in inbound)
    assert any("message content" in l for l in inbound)
    assert any(l.startswith("250 2.0.0 Ok: relayed as #1") for l in inbound)
    assert any(l.lower().startswith("mail from:<relay@example.de>") for l in outbound)
    assert any("queued as ABC123" in l for l in outbound)
    assert sessions["inbound"]["helo"] == "nas.home.lan"


def test_sender_rejected(env):
    with pytest.raises(smtplib.SMTPSenderRefused):
        env.send(mail_from="evil@example.com")
    assert not env.upstream.messages
    m = env.message()
    assert m["status"] == "rejected" and m["mail_from"] == "evil@example.com"


def test_permanent_rejection_is_returned_and_notified(env):
    env.settings.update({"notify_address": "admin@example.de"})
    env.upstream.reject_rcpt.add("bad@example.de")
    with pytest.raises(smtplib.SMTPDataError) as exc:
        env.send(mail_from="x@home.lan", rcpts=["bad@example.de"])
    assert exc.value.smtp_code == 550
    m = env.message()
    assert m["status"] == "failed"
    assert "No such user" in m["failed_rcpts"]
    note = env.db.query_one("SELECT * FROM messages WHERE is_notification = 1")
    assert note["status"] == "queued"
    assert json.loads(note["rcpt_to"]) == ["admin@example.de"]
    assert note["subject"] == "Undeliverable: Backup ok"


def test_network_rejected(env):
    env.settings.update({"allowed_networks": ["10.0.0.0/8"]})
    with pytest.raises(smtplib.SMTPConnectError):
        smtplib.SMTP("127.0.0.1", env.port)
    s = env.db.query_one("SELECT * FROM sessions")
    assert s["result"] == "rejected: network"


def test_relay_down_queues_and_retries(env):
    env.relay_down()
    before = time.time()
    assert env.send() == {}  # accepted by smtp-relay despite relay outage
    m = env.message()
    assert m["status"] == "queued"
    assert m["attempts"] == 1
    assert before + 55 < m["next_attempt_at"] < time.time() + 65  # first interval: 1 minute
    assert env.run(env.queue.process_due()) == 0  # not due yet

    env.relay_up()
    env.make_due()
    assert env.run(env.queue.process_due()) == 1
    m = env.message()
    assert m["status"] == "relayed" and m["attempts"] == 2 and m["next_attempt_at"] is None
    assert len(env.upstream.messages) == 1
    assert len(env.db.query("SELECT * FROM sessions WHERE message_id = 1")) == 2
    inbound = [l["line"] for l in env.db.transcript(m["session_id"])]
    reply = next(l for l in inbound if l.startswith("250 2.0.0 Ok: queued as #1"))
    assert "?" not in reply and " - next attempt" in reply


def test_retry_intervals_grow(env):
    env.relay_down()
    env.settings.update({"retry_intervals": "1, 10"})
    env.send()
    for expected in (10, 10):
        env.make_due()
        now = time.time()
        env.run(env.queue.process_due())
        m = env.message()
        assert m["status"] == "queued"
        assert abs(m["next_attempt_at"] - now - expected * 60) < 5
    assert env.message()["attempts"] == 3


def test_temporary_recipient_failure_retries_only_that_recipient(env):
    env.upstream.temp_rcpt.add("later@example.de")
    env.send(rcpts=["ok@example.de", "later@example.de"])
    m = env.message()
    assert m["status"] == "queued"
    assert json.loads(m["pending_rcpts"]) == ["later@example.de"]
    assert json.loads(m["delivered_rcpts"]) == ["ok@example.de"]
    assert env.upstream.messages[0].rcpt_tos == ["ok@example.de"]

    env.upstream.temp_rcpt.clear()
    env.make_due()
    env.run(env.queue.process_due())
    m = env.message()
    assert m["status"] == "relayed"
    assert env.upstream.messages[1].rcpt_tos == ["later@example.de"]
    assert json.loads(m["delivered_rcpts"]) == ["ok@example.de", "later@example.de"]


def test_queue_lifetime_exceeded(env):
    env.settings.update({"notify_address": "admin@example.de"})
    env.relay_down()
    env.send()
    env.db.execute("UPDATE messages SET queued_at = ? WHERE id = 1", (time.time() - 49 * 3600,))
    env.make_due()
    env.run(env.queue.process_due())
    m = env.message()
    assert m["status"] == "failed"
    assert "lifetime" in m["failed_rcpts"]
    notes = env.db.query("SELECT * FROM messages WHERE is_notification = 1")
    assert len(notes) == 1  # no notification about the (also failing) notification
    assert notes[0]["status"] == "queued"


def test_recover_interrupted_delivery(env):
    message_id = env.queue.accept(raw=MSG.encode(), mail_from="nas@intern.lan", rcpts=["me@example.de"])
    assert env.message(message_id)["status"] == "relaying"
    env.queue.recover()
    assert env.message(message_id)["status"] == "queued"
    env.run(env.queue.process_due())
    assert env.message(message_id)["status"] == "relayed"


def test_paused_queue(env):
    env.relay_down()
    env.send()
    env.relay_up()
    env.settings.update({"queue_paused": True})
    env.make_due()
    assert env.run(env.queue.process_due()) == 0
    assert env.run(env.queue.deliver_now(1)).status == "relayed"  # manual delivery ignores the pause


def test_content_dropped_after_delivery_when_not_stored(env):
    env.settings.update({"store_content": False})
    env.relay_down()
    env.send()
    assert env.message()["raw"] is not None  # kept while queued
    env.relay_up()
    env.make_due()
    env.run(env.queue.process_due())
    m = env.message()
    assert m["status"] == "relayed" and m["raw"] is None


def test_web_ui(env):
    env.send()
    env.relay_down()
    env.send()
    client = web_client(env)
    for url in ["/", "/?q=Backup", "/?status=queued", "/messages/1", "/messages/2", "/queue", "/sessions",
                "/sessions/1", "/sessions?kind=outbound", "/settings", "/tools", "/relays", "/relays/new", "/relays/1", "/messages/1/eml", "/healthz"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Backup ok" in client.get("/").text
    page = client.get("/messages/1").text
    assert "from:&lt;nas@intern.lan&gt;" in page and "queued as ABC123" in page
    assert "Deliver now" in client.get("/messages/2").text
    assert 'class="count">1<' in client.get("/queue").text

    r = client.post("/queue/pause", data={"paused": "1"}, follow_redirects=False)
    assert r.status_code == 303 and env.settings.get("queue_paused") is True
    r = client.post("/settings", data={"allowed_networks": "127.0.0.0/8", "smtp_hostname": "x",
                                       "max_message_size_mb": "10", "relay_timeout": "5", "retention_days": "7",
                                       "retry_intervals": "2,4", "queue_max_age_hours": "24", "notify_address": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert env.settings.get("relay_timeout") == 5
    assert env.settings.get("retry_intervals") == "2, 4"
    assert env.settings.get("queue_paused") is True  # not touched by the settings form

    env.relay_up()
    r = client.post("/queue/2/deliver", follow_redirects=False)
    assert r.status_code == 303 and env.message(2)["status"] == "relayed"
    r = client.post("/messages/1/resend", follow_redirects=False)
    assert r.status_code == 303
    assert len(env.db.query("SELECT * FROM sessions WHERE message_id = 1")) == 2


def test_web_remove_from_queue(env):
    env.relay_down()
    env.send()
    client = web_client(env)
    r = client.post("/queue/1/remove", follow_redirects=False)
    assert r.status_code == 303
    m = env.message()
    assert m["status"] == "failed" and "Manually" in m["failed_rcpts"]
    assert client.post("/queue/deliver-all", follow_redirects=False).status_code == 303


def test_routing_to_second_relay(env, tmp_path):
    """Senders are routed to the relay with the most specific matching pattern, each with its own rewriting."""
    second = Upstream()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ctrl = Controller(second, hostname="127.0.0.1", port=port)
    ctrl.start()
    try:
        _, errors = env.queue.relays.save({"name": "other", "host": "127.0.0.1", "port": port, "security": "none",
                                           "senders": ["printer@home.lan"]})
        assert not errors
        env.send(mail_from="printer@home.lan")   # exact pattern on "other" beats @home.lan on "upstream"
        env.send(mail_from="camera@home.lan")
        assert [m.mail_from for m in second.messages] == ["printer@home.lan"]  # no rewriting on "other"
        assert [m.mail_from for m in env.upstream.messages] == ["relay@example.de"]
        names = [r["relay_name"] for r in env.db.query("SELECT relay_name FROM messages ORDER BY id")]
        assert names == ["other", "upstream"]
    finally:
        ctrl.stop()


def test_queued_message_follows_relay_changes(env):
    """Routing happens on every attempt: a queued message uses the relay configured at retry time."""
    env.relay_down()
    env.send(mail_from="camera@home.lan")
    assert env.message()["status"] == "queued"
    env.queue.relays.delete(env.relay_id)
    env.make_due()
    env.run(env.queue.process_due())
    m = env.message()
    assert m["status"] == "failed" and "No enabled relay matches the sender" in m["failed_rcpts"]


def test_test_message_with_fixed_relay(env):
    client = web_client(env)
    r = client.post("/tools/send-test", data={"relay_id": str(env.relay_id), "from": "someone@else.org",
                                              "to": "me@example.de"}, follow_redirects=False)
    assert r.status_code == 303
    assert env.message()["status"] == "relayed" and env.message()["relay_id"] == env.relay_id


def test_web_relay_management(env):
    client = web_client(env)
    r = client.post("/relays/new", data={"name": "second", "host": "smtp.example.net", "port": "587",
                                         "security": "starttls", "senders": "@other.lan", "enabled": "on",
                                         "tls_verify": "on"}, follow_redirects=False)
    assert r.status_code == 303
    relay = env.queue.relays.route("x@other.lan")[0]
    assert relay["name"] == "second"
    r = client.post(f"/relays/{relay['id']}", data={"name": "second", "host": "smtp.example.net", "port": "587",
                                                    "security": "starttls", "senders": "@home.lan", "enabled": "on"})
    assert "already used by relay" in r.text
    assert "second" in client.get("/relays").text
    client.post(f"/relays/{relay['id']}/delete")
    assert env.queue.relays.route("x@other.lan") == (None, None)
