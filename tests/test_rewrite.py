import email
import email.policy

from app.config import DEFAULTS
from app.relays import NEW_RELAY, as_settings
from app.rewrite import header_meta, prepare

RAW = (b"From: =?utf-8?q?Zo=C3=AB=27s_NAS?= <nas@intern.lan>\r\n"
       b"To: me@example.de\r\n"
       b"Subject: =?utf-8?q?R=C3=A9sum=C3=A9?=\r\n"
       b"\r\n"
       b"Body line 1\r\nBody line 2\r\n")


def settings(**kw):
    return {**DEFAULTS, **as_settings({**NEW_RELAY}), **kw}


def parse(data):
    return email.message_from_bytes(data, policy=email.policy.default)


def test_header_meta():
    meta = header_meta(RAW)
    assert meta["subject"] == "Résumé"
    assert "nas@intern.lan" in meta["from_header"]


def test_no_rewrite_keeps_message():
    data, env_from, notes = prepare(RAW, "nas@intern.lan", settings(add_missing_headers=False))
    assert data == RAW
    assert env_from == "nas@intern.lan"
    assert notes == []


def test_rewrite_keep_name():
    s = settings(rewrite_enabled=True, rewrite_from_address="relay@example.de", add_missing_headers=False)
    data, env_from, notes = prepare(RAW, "nas@intern.lan", s)
    msg = parse(data)
    assert msg["From"].addresses[0].addr_spec == "relay@example.de"
    assert msg["From"].addresses[0].display_name == "Zoë's NAS"
    assert "nas@intern.lan" in str(msg["Reply-To"])
    assert "nas@intern.lan" in str(msg["X-Original-From"])
    assert env_from == "relay@example.de"
    assert data.endswith(b"\r\n\r\nBody line 1\r\nBody line 2\r\n")
    assert notes


def test_rewrite_address_mode_without_envelope():
    s = settings(rewrite_enabled=True, rewrite_from_address="relay@example.de", rewrite_name_mode="address",
                 rewrite_envelope=False, rewrite_set_reply_to=False)
    data, env_from, _ = prepare(RAW, "nas@intern.lan", s)
    msg = parse(data)
    assert msg["From"].addresses[0].display_name == "nas@intern.lan"
    assert msg["Reply-To"] is None
    assert env_from == "nas@intern.lan"


def test_missing_headers_and_received():
    raw = b"Subject: hi\r\n\r\nbody\r\n"
    data, _, notes = prepare(raw, "a@b.de", settings(),
                             {"helo": "nas", "client_ip": "192.168.1.5", "client_name": None, "id": 1, "rcpt": "x@y.de"})
    msg = parse(data)
    assert msg["Date"] and msg["Message-ID"]
    assert data.startswith(b"Received: from nas (unknown [192.168.1.5])")
    assert len(notes) == 2


def test_headers_only_message():
    data, _, _ = prepare(b"Subject: x", "a@b.de", settings(add_missing_headers=False))
    assert data == b"Subject: x\r\n\r\n"
