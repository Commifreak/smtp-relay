"""Relay management, sender-based routing and migration of the single-relay configuration."""
import json

import pytest

from app.migrations import open_database
from app.relays import Relays


@pytest.fixture
def relays(tmp_path):
    return Relays(open_database(str(tmp_path / "t.db")))


def add(relays, name, senders, **kw):
    relay_id, errors = relays.save({"name": name, "host": f"smtp.{name}.example", "port": 465, "security": "ssl",
                                    "senders": senders, **kw})
    assert not errors, errors
    return relay_id


def test_most_specific_pattern_wins(relays):
    catch_all = add(relays, "fallback", ["*"])
    domain = add(relays, "home", ["@home.example.com"])
    exact = add(relays, "nas", ["nas@home.example.com"])
    assert relays.route("nas@home.example.com")[0]["id"] == exact
    assert relays.route("Printer@Home.Example.com")[0]["id"] == domain
    relay, pattern = relays.route("anyone@elsewhere.org")
    assert relay["id"] == catch_all and pattern == "*"


def test_no_match_and_disabled(relays):
    relay_id = add(relays, "home", ["@home.example.com"])
    assert relays.route("x@other.example.com") == (None, None)
    assert relays.route("x@sub.home.example.com") == (None, None)  # no subdomain matching
    relays.save({"enabled": False}, relay_id)
    assert relays.route("x@home.example.com") == (None, None)


def test_validation(relays):
    add(relays, "home", ["@home.example.com"])
    _, errors = relays.save({"name": "", "host": "", "port": "99999", "security": "x", "senders": "bad pattern",
                             "rewrite_enabled": True, "rewrite_from_address": ""})
    assert len(errors) == 6
    _, errors = relays.save({"name": "dup", "host": "h", "port": 25, "security": "none",
                             "senders": "@HOME.example.com"})
    assert errors == ["Sender pattern @home.example.com is already used by relay “home”"]
    _, errors = relays.save({"name": "dup", "host": "h", "port": 25, "security": "none", "senders": "@home.example.com",
                             "enabled": False})
    assert not errors  # disabled relays may overlap


def test_password_kept_when_empty(relays):
    relay_id = add(relays, "home", ["@home.example.com"], password="secret")
    relays.save({"password": "", "port": 587}, relay_id)
    relay = relays.get(relay_id)
    assert relay["password"] == "secret" and relay["port"] == 587


def test_migrate_single_relay(relays):
    relays.db.save_settings({"relay_host": "smtp.mailbox.org", "relay_port": 587, "relay_security": "starttls",
                             "relay_username": "me@mailbox.org", "relay_password": "pw",
                             "allowed_senders": ["nas@example.com"], "rewrite_enabled": True,
                             "rewrite_from_address": "relay@example.com", "retention_days": 7})
    assert relays.migrate_single_relay()
    relay = relays.first()
    assert relay["name"] == "smtp.mailbox.org" and relay["port"] == 587 and relay["security"] == "starttls"
    assert relay["password"] == "pw" and relay["senders"] == ["nas@example.com"]
    assert relay["rewrite_enabled"] and relay["rewrite_from_address"] == "relay@example.com"
    stored = relays.db.load_settings()
    assert "relay_host" not in stored and "allowed_senders" not in stored and stored["retention_days"] == 7
    relays.delete(relay["id"])
    assert not relays.migrate_single_relay()  # deleted relays do not come back
