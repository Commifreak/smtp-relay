"""End-to-end: client -> MTA-Relay -> fake upstream relay."""
import asyncio
import json
import smtplib
import socket
import threading

import pytest
from aiosmtpd.controller import Controller
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.smtp_server import protocol_factory
from app.web import create_app


class Upstream:
    def __init__(self):
        self.messages = []
        self.reject_rcpt = set()

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if address in self.reject_rcpt:
            return "550 5.1.1 No such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope)
        return "250 2.0.0 Ok: queued as ABC123"


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
    assert not settings.update({"relay_host": "127.0.0.1", "relay_port": up_port, "relay_security": "none",
                                "allowed_senders": ["nas@intern.lan", "@home.lan"],
                                "allowed_networks": ["127.0.0.0/8"],
                                "rewrite_enabled": True, "rewrite_from_address": "relay@example.de"})
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    server = asyncio.run_coroutine_threadsafe(
        loop.create_server(protocol_factory(db, settings), "127.0.0.1", 0), loop).result()
    port = server.sockets[0].getsockname()[1]
    yield db, settings, upstream, port
    loop.call_soon_threadsafe(server.close)
    loop.call_soon_threadsafe(loop.stop)
    ctrl.stop()


MSG = "From: NAS <nas@intern.lan>\r\nTo: me@example.de\r\nSubject: Backup ok\r\n\r\nAll good.\r\n"


def test_relay_success(env):
    db, settings, upstream, port = env
    with smtplib.SMTP("127.0.0.1", port) as c:
        c.ehlo("nas.home.lan")
        c.sendmail("nas@intern.lan", ["me@example.de"], MSG)
    assert len(upstream.messages) == 1
    env_up = upstream.messages[0]
    assert env_up.mail_from == "relay@example.de"
    assert b"From: NAS <relay@example.de>" in env_up.content
    assert b"X-Original-From: NAS <nas@intern.lan>" in env_up.content
    m = db.query_one("SELECT * FROM messages")
    assert m["status"] == "relayed"
    assert m["subject"] == "Backup ok"
    assert "queued as ABC123" in m["detail"]
    sessions = {s["kind"]: s for s in db.query("SELECT * FROM sessions")}
    inbound = [l["line"] for l in db.transcript(sessions["inbound"]["id"])]
    outbound = [l["line"] for l in db.transcript(sessions["outbound"]["id"])]
    assert any(l.lower().startswith("ehlo nas.home.lan") for l in inbound)
    assert any(l.lower().startswith("mail from:<nas@intern.lan>") for l in inbound)
    assert any("Nachrichteninhalt" in l for l in inbound)
    assert any(l.startswith("250 2.0.0 Ok: relayed") for l in inbound)
    assert any(l.lower().startswith("mail from:<relay@example.de>") for l in outbound)
    assert any("queued as ABC123" in l for l in outbound)
    assert sessions["inbound"]["helo"] == "nas.home.lan"


def test_sender_rejected(env):
    db, settings, upstream, port = env
    with smtplib.SMTP("127.0.0.1", port) as c:
        with pytest.raises(smtplib.SMTPSenderRefused):
            c.sendmail("evil@example.com", ["me@example.de"], MSG)
    assert not upstream.messages
    m = db.query_one("SELECT * FROM messages")
    assert m["status"] == "rejected" and m["mail_from"] == "evil@example.com"


def test_upstream_rejects_recipient(env):
    db, settings, upstream, port = env
    upstream.reject_rcpt.add("bad@example.de")
    with smtplib.SMTP("127.0.0.1", port) as c:
        with pytest.raises(smtplib.SMTPDataError) as exc:
            c.sendmail("x@home.lan", ["bad@example.de"], MSG)
    assert exc.value.smtp_code == 550
    m = db.query_one("SELECT * FROM messages")
    assert m["status"] == "failed"


def test_network_rejected(env):
    db, settings, upstream, port = env
    settings.update({"allowed_networks": ["10.0.0.0/8"]})
    with pytest.raises(smtplib.SMTPConnectError):
        smtplib.SMTP("127.0.0.1", port)
    s = db.query_one("SELECT * FROM sessions")
    assert s["result"] == "abgelehnt: Netz"


def test_upstream_down_defers(env):
    db, settings, upstream, port = env
    settings.update({"relay_port": 1})
    with smtplib.SMTP("127.0.0.1", port) as c:
        with pytest.raises(smtplib.SMTPDataError) as exc:
            c.sendmail("nas@intern.lan", ["me@example.de"], MSG)
    assert exc.value.smtp_code == 451
    assert db.query_one("SELECT status FROM messages")["status"] == "deferred"


def test_web_ui(env):
    db, settings, upstream, port = env
    with smtplib.SMTP("127.0.0.1", port) as c:
        c.sendmail("nas@intern.lan", ["me@example.de"], MSG)
    client = TestClient(create_app(db, settings))
    for url in ["/", "/?q=Backup", "/?status=relayed", "/messages/1", "/sessions", "/sessions/1",
                "/sessions?kind=outbound", "/settings", "/tools", "/messages/1/eml", "/healthz"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Backup ok" in client.get("/").text
    page = client.get("/messages/1").text
    assert "from:&lt;nas@intern.lan&gt;" in page and "queued as ABC123" in page
    r = client.post("/settings", data={"allowed_senders": "a@b.de", "allowed_networks": "127.0.0.0/8",
                                       "relay_host": "127.0.0.1", "relay_port": "25", "relay_security": "none",
                                       "smtp_hostname": "x", "max_message_size_mb": "10", "relay_timeout": "5",
                                       "retention_days": "7", "rewrite_name_mode": "keep"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert settings.get("allowed_senders") == ["a@b.de"]
    assert settings.get("rewrite_enabled") is False
    r = client.post("/messages/1/resend", follow_redirects=False)
    assert r.status_code == 303
    assert len(db.query("SELECT * FROM sessions WHERE message_id = 1")) == 2
