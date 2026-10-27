"""Web UI authentication: local admin account, login sessions and trusted proxy headers (e.g. Authelia)."""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import string
import time

from .config import Settings, ip_allowed
from .db import Database

log = logging.getLogger("smtp-relay.auth")

SESSION_COOKIE = "smtp_relay_session"
SESSION_LIFETIME = 7 * 86400  # sliding expiry
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P,
                                         base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    alphabet = "".join(c for c in alphabet if c not in "lIO0")  # avoid look-alikes in the log
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_credentials(settings: Settings, reset: bool = False) -> None:
    """Create initial credentials on first start (or on reset) and print them to stdout."""
    s = settings.snapshot()
    if reset or not s["auth_password_hash"]:
        password = generate_password()
        settings.set_internal(auth_username=s["auth_username"] or "admin",
                              auth_password_hash=hash_password(password), auth_initial_password=password)
        log.info("Generated new initial password%s", " (reset requested)" if reset else "")
    s = settings.snapshot()
    if s["auth_initial_password"]:
        port = os.environ.get("WEB_PORT", "8080")
        print("\n".join([
            "",
            "=" * 64,
            "  smtp-relay – initial login",
            f"    URL:       http://<docker-host>:{port}",
            f"    Username:  {s['auth_username']}",
            f"    Password:  {s['auth_initial_password']}",
            "  The setup wizard asks you to choose your own credentials.",
            "=" * 64,
            "",
        ]), flush=True)


def set_credentials(settings: Settings, username: str, password: str) -> list[str]:
    errors = []
    username = username.strip()
    if not username or any(c.isspace() for c in username):
        errors.append("Username must not be empty or contain spaces")
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    if not errors:
        settings.set_internal(auth_username=username, auth_password_hash=hash_password(password),
                              auth_initial_password="")
    return errors


def check_login(settings: Settings, username: str, password: str) -> bool:
    s = settings.snapshot()
    ok_user = hmac.compare_digest(username.strip().encode(), s["auth_username"].encode())
    ok_pass = verify_password(password, s["auth_password_hash"])
    return ok_user and ok_pass


class Sessions:
    def __init__(self, db: Database):
        self.db = db

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute("INSERT INTO web_sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                        (self._hash(token), now, now + SESSION_LIFETIME))
        self.db.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        cur = self.db.execute("UPDATE web_sessions SET expires_at = ? WHERE token_hash = ? AND expires_at >= ?",
                              (now + SESSION_LIFETIME, self._hash(token), now))
        return cur.rowcount == 1

    def delete(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (self._hash(token),))

    def delete_others(self, token: str | None) -> None:
        self.db.execute("DELETE FROM web_sessions WHERE token_hash != ?", (self._hash(token or ""),))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


def proxy_user(settings: Settings, peer_ip: str, headers) -> str | None:
    """User name from a trusted reverse proxy's auth header (Authelia: Remote-User), else None."""
    s = settings.snapshot()
    if not s["proxy_auth_enabled"] or not ip_allowed(peer_ip, s["proxy_auth_trusted_networks"]):
        return None
    user = headers.get(s["proxy_auth_header"], "").strip()
    return user or None
