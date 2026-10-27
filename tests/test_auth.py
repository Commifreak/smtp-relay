"""Login, setup wizard, account management and reverse proxy authentication."""
import pytest
from fastapi.testclient import TestClient

from app.auth import ensure_credentials, hash_password, set_credentials, verify_password
from app.config import Settings
from app.db import Database
from app.delivery import DeliveryQueue
from app.web import create_app

PROXY_IP = "172.18.0.5"


@pytest.fixture
def settings(tmp_path):
    return Settings(Database(str(tmp_path / "t.db")))


def client_for(settings, ip="192.168.1.20"):
    app = create_app(settings.db, settings, DeliveryQueue(settings.db, settings))
    return TestClient(app, client=(ip, 50000))


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_password_hashing():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")


def test_initial_credentials_printed_until_changed(settings, capsys):
    ensure_credentials(settings)
    out = capsys.readouterr().out
    initial = settings.get("auth_initial_password")
    assert settings.get("auth_username") == "admin"
    assert len(initial) == 16 and initial in out
    assert verify_password(initial, settings.get("auth_password_hash"))

    ensure_credentials(settings)  # restart before setup: same password, printed again
    assert settings.get("auth_initial_password") == initial
    assert initial in capsys.readouterr().out

    set_credentials(settings, "me", "my-own-password")
    ensure_credentials(settings)  # restart after setup: nothing printed
    assert capsys.readouterr().out == ""

    ensure_credentials(settings, reset=True)
    new = settings.get("auth_initial_password")
    assert new and new != initial and settings.get("auth_username") == "me"
    assert new in capsys.readouterr().out


def test_login_required(settings):
    ensure_credentials(settings)
    client = client_for(settings)
    r = client.get("/messages/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fmessages%2F1"
    assert client.post("/queue/deliver-all", follow_redirects=False).status_code == 401
    assert client.get("/healthz").status_code == 200
    assert "container log" in client.get("/login").text


def test_wrong_password(settings):
    ensure_credentials(settings)
    r = login(client_for(settings), "admin", "wrong")
    assert r.status_code == 200 and "Invalid username or password" in r.text


def test_open_redirect_blocked(settings):
    ensure_credentials(settings)
    client = client_for(settings)
    r = client.post("/login", data={"username": "admin", "password": settings.get("auth_initial_password"),
                                    "next": "//evil.example.com"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_setup_wizard(settings):
    ensure_credentials(settings)
    initial = settings.get("auth_initial_password")
    client = client_for(settings)
    assert login(client, "admin", initial).status_code == 303
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    assert client.get("/setup").url.path == "/setup/account"

    # account: generated password must be replaced, passwords must match
    r = client.post("/setup/account", data={"username": "admin", "password": initial, "password2": initial})
    assert "choose your own password" in r.text
    r = client.post("/setup/account", data={"username": "admin", "password": "abcdefgh1", "password2": "x"})
    assert "do not match" in r.text
    r = client.post("/setup/account", data={"username": "me", "password": "abcdefgh1", "password2": "abcdefgh1",
                                            "proxy_auth_header": "Remote-User"}, follow_redirects=False)
    assert r.headers["location"] == "/setup/relay"
    assert settings.get("auth_username") == "me" and settings.get("auth_initial_password") == ""

    r = client.post("/setup/relay", data={"relay_host": "smtp.example.net", "relay_port": "587",
                                          "relay_security": "starttls", "relay_username": "me@example.net",
                                          "relay_password": "pw", "relay_tls_verify": "on"}, follow_redirects=False)
    assert r.headers["location"] == "/setup/senders"
    assert settings.get("relay_port") == 587 and settings.get("relay_password") == "pw"

    r = client.post("/setup/senders", data={"allowed_senders": "nas@example.com\n@iot.example.com",
                                            "allowed_networks": "192.168.0.0/16"}, follow_redirects=False)
    assert r.headers["location"] == "/setup/options"

    r = client.post("/setup/options", data={"rewrite_enabled": "on", "rewrite_from_address": "",
                                            "rewrite_name_mode": "keep"})
    assert "valid sender address" in r.text
    r = client.post("/setup/options", data={"rewrite_enabled": "on", "rewrite_from_address": "relay@example.net",
                                            "rewrite_name_mode": "address", "notify_address": "me@example.net"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"
    assert settings.get("setup_completed") is True
    assert settings.get("allowed_senders") == ["nas@example.com", "@iot.example.com"]

    assert client.get("/").status_code == 200
    assert client.get("/setup/account", follow_redirects=False).headers["location"] == "/"
    assert login(client_for(settings), "admin", initial).status_code == 200  # old credentials are gone
    assert login(client_for(settings), "me", "abcdefgh1").status_code == 303


def test_change_account(settings):
    set_credentials(settings, "admin", "first-password")
    settings.set_internal(setup_completed=True)
    client = client_for(settings)
    login(client, "admin", "first-password")
    other = client_for(settings)
    login(other, "admin", "first-password")

    data = {"username": "admin2", "current_password": "nope", "new_password": "second-pass",
            "new_password2": "second-pass"}
    assert "Current password is wrong" in client.post("/settings/account", data=data).text
    data["current_password"] = "first-password"
    r = client.post("/settings/account", data=data, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/settings").status_code == 200  # own session stays valid
    assert other.get("/settings", follow_redirects=False).status_code == 303  # other sessions are signed out
    assert login(client_for(settings), "admin2", "second-pass").status_code == 303


def test_logout(settings):
    set_credentials(settings, "admin", "first-password")
    settings.set_internal(setup_completed=True)
    client = client_for(settings)
    login(client, "admin", "first-password")
    assert client.post("/logout", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_proxy_auth(settings):
    set_credentials(settings, "admin", "first-password")
    settings.set_internal(setup_completed=True)
    headers = {"Remote-User": "alice"}

    # disabled: header is ignored
    assert client_for(settings, PROXY_IP).get("/", headers=headers, follow_redirects=False).status_code == 303

    assert not settings.update({"proxy_auth_enabled": True, "proxy_auth_trusted_networks": ["172.18.0.0/16"],
                                "proxy_auth_logout_url": "https://auth.example.com/logout"})
    proxied = client_for(settings, PROXY_IP)
    r = proxied.get("/", headers=headers)
    assert r.status_code == 200 and "alice" in r.text
    r = proxied.post("/logout", headers=headers, follow_redirects=False)
    assert r.headers["location"] == "https://auth.example.com/logout"

    # spoofed header from an untrusted address, or no header at all
    assert client_for(settings, "192.168.1.20").get("/", headers=headers, follow_redirects=False).status_code == 303
    assert proxied.get("/", follow_redirects=False).status_code == 303

    # proxy auth needs trusted networks
    assert settings.update({"proxy_auth_trusted_networks": []})
