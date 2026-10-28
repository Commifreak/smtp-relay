"""Runtime settings: defaults, validation and address matching."""
import ipaddress
import re
import threading

from .db import Database

# Settings editable in the web UI (setup wizard and settings page).
DEFAULTS: dict = {
    "smtp_hostname": "smtp-relay",
    "allowed_networks": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"],
    "allowed_recipients": [],
    "max_message_size_mb": 25,
    "relay_timeout": 30,  # connection details, sender patterns and rewriting are configured per relay
    "add_received_header": True,
    "add_missing_headers": True,
    "store_content": True,
    "retention_days": 30,
    "retry_intervals": "1, 5, 15, 30, 60",  # minutes; the last value repeats
    "queue_max_age_hours": 48,
    "queue_paused": False,
    "alert_address": "",               # receives alert mails; empty = alerts off
    "alert_relay_id": 0,               # relay for alert mails; 0 = automatic
    "alert_from_address": "",          # sender of alert mails; empty = derived from the relay
    "alert_delivery_failed": True,
    "alert_relay_down": True,
    "alert_queue_backlog": True,
    "alert_queue_backlog_minutes": 30,
    "alert_sender_rejected": True,
    "alert_client_rejected": False,
    "alert_login_failed": True,
    "public_url": "",                  # web UI address for links in alert mails
    "proxy_auth_enabled": False,
    "proxy_auth_header": "Remote-User",
    "proxy_auth_trusted_networks": [],
    "proxy_auth_logout_url": "",
}

# State managed by the application itself, never taken from settings forms.
INTERNAL: dict = {
    "setup_completed": False,
    "auth_username": "",
    "auth_password_hash": "",
    "auth_initial_password": "",  # generated password, kept until the admin sets their own
}

LIST_KEYS = {"allowed_networks", "allowed_recipients", "proxy_auth_trusted_networks"}
BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}
SECURITY_MODES = ("ssl", "starttls", "none")
NAME_MODES = ("keep", "address", "fixed", "none")

ADDRESS_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
PATTERN_RE = re.compile(r"^(\*|(\*?@|[^@\s<>]+@)[^@\s<>]+)$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]+$")


def split_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r"[\r\n,;]+", value or "") if p.strip()]


def validate(values: dict) -> tuple[dict, list[str]]:
    """Normalize a (partial) settings dict. Returns (clean values, error messages)."""
    clean, errors = {}, []
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        try:
            if key in LIST_KEYS:
                value = split_list(value)
            elif key in BOOL_KEYS:
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
            elif key in INT_KEYS:
                value = int(value)
            else:
                value = str(value).strip()
        except (TypeError, ValueError):
            errors.append(f"{key}: invalid value")
            continue
        clean[key] = value

    for net in clean.get("allowed_networks", []) + clean.get("proxy_auth_trusted_networks", []):
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            errors.append(f"Invalid network: {net}")
    if "allowed_recipients" in clean:
        clean["allowed_recipients"] = [p.lower() for p in clean["allowed_recipients"]]
        for p in clean["allowed_recipients"]:
            if not PATTERN_RE.match(p):
                errors.append(f"Invalid address pattern: {p}")
    if "retry_intervals" in clean:
        try:
            clean["retry_intervals"] = ", ".join(str(m) for m in parse_intervals(clean["retry_intervals"]))
        except ValueError:
            errors.append("Retry intervals: enter comma-separated minutes greater than 0, e.g. 1, 5, 15, 60")
    if clean.get("alert_address") and not ADDRESS_RE.match(clean["alert_address"]):
        errors.append("Invalid alert address")
    if clean.get("alert_from_address") and not ADDRESS_RE.match(clean["alert_from_address"]):
        errors.append("Invalid alert sender address")
    if "alert_queue_backlog_minutes" in clean and clean["alert_queue_backlog_minutes"] < 1:
        errors.append("The queue alert needs at least 1 minute")
    public_url = clean.get("public_url")
    if public_url and not public_url.startswith(("http://", "https://")):
        errors.append("Web UI address must start with http:// or https://")
    elif public_url:
        clean["public_url"] = public_url.rstrip("/")
    if "queue_max_age_hours" in clean and clean["queue_max_age_hours"] < 1:
        errors.append("Maximum queue lifetime must be at least 1 hour")
    if "proxy_auth_header" in clean and not HEADER_RE.match(clean["proxy_auth_header"]):
        errors.append("Invalid proxy auth header name")
    if clean.get("proxy_auth_enabled") and not clean.get("proxy_auth_trusted_networks"):
        errors.append("Proxy authentication needs at least one trusted proxy network")
    logout = clean.get("proxy_auth_logout_url")
    if logout and not logout.startswith(("http://", "https://")):
        errors.append("Logout URL must start with http:// or https://")
    for key in ("retention_days", "max_message_size_mb", "relay_timeout"):
        if key in clean and clean[key] < 0:
            errors.append(f"{key} must not be negative")
    return clean, errors


class Settings:
    """Thread-safe settings store backed by the database."""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        stored = db.load_settings()
        self._data = {**DEFAULTS, **INTERNAL, **{k: v for k, v in stored.items() if k in DEFAULTS or k in INTERNAL}}
        db.save_settings(self._data)

    def get(self, key: str):
        with self._lock:
            return self._data[key]

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def set_internal(self, **values) -> None:
        unknown = set(values) - set(INTERNAL)
        if unknown:
            raise KeyError(f"Not an internal setting: {', '.join(unknown)}")
        with self._lock:
            self._data.update(values)
        self.db.save_settings(values)

    def update(self, values: dict) -> list[str]:
        clean, errors = validate(values)
        if errors:
            return errors
        merged = {**self.snapshot(), **clean}
        _, errors = validate(merged)
        if errors:
            return errors
        with self._lock:
            self._data = merged
        self.db.save_settings(merged)
        return []


def parse_intervals(value: str) -> list[int]:
    """Parse '1, 5, 15' into minutes. Raises ValueError on invalid input."""
    minutes = [int(p) for p in re.split(r"[,;\s]+", str(value)) if p]
    if not minutes or any(m <= 0 for m in minutes):
        raise ValueError(value)
    return minutes


def address_matches(address: str, patterns: list[str]) -> bool:
    """Match an address against patterns: 'user@dom', '@dom', '*@dom' or '*'."""
    address = address.strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    for p in patterns:
        if p == "*":
            return True
        if p.startswith("*@"):
            p = p[1:]
        if p.startswith("@"):
            if domain == p[1:]:
                return True
        elif address == p:
            return True
    return False


def ip_allowed(ip: str, networks: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    for net in networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except (ValueError, TypeError):
            continue
    return False


def normalize_ip(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6 and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(addr)
    except ValueError:
        return ip
