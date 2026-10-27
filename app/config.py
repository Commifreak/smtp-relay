"""Runtime settings: defaults, environment seeding, validation and address matching."""
import ipaddress
import os
import re
import threading

from .db import Database

# Settings editable in the web UI. Environment variables (upper-case key) only seed
# the initial value; afterwards the value stored in the database wins.
DEFAULTS: dict = {
    "smtp_hostname": "mta-relay",
    "allowed_networks": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"],
    "allowed_senders": [],
    "allowed_recipients": [],
    "max_message_size_mb": 25,
    "relay_host": "smtp.mailbox.org",
    "relay_port": 465,
    "relay_security": "ssl",  # ssl | starttls | none
    "relay_tls_verify": True,
    "relay_username": "",
    "relay_password": "",
    "relay_timeout": 30,
    "rewrite_enabled": False,
    "rewrite_from_address": "",
    "rewrite_name_mode": "keep",  # keep | address | fixed | none
    "rewrite_fixed_name": "",
    "rewrite_set_reply_to": True,
    "rewrite_envelope": True,
    "add_received_header": True,
    "add_missing_headers": True,
    "store_content": True,
    "retention_days": 30,
    "retry_intervals": "1, 5, 15, 30, 60",  # minutes; the last value repeats
    "queue_max_age_hours": 48,
    "queue_paused": False,
    "notify_address": "",
}

LIST_KEYS = {"allowed_networks", "allowed_senders", "allowed_recipients"}
BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}
SECURITY_MODES = ("ssl", "starttls", "none")
NAME_MODES = ("keep", "address", "fixed", "none")

ADDRESS_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
PATTERN_RE = re.compile(r"^(\*|(\*?@|[^@\s<>]+@)[^@\s<>]+)$")


def _parse_env(key: str, raw: str):
    default = DEFAULTS[key]
    if key in LIST_KEYS:
        return [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "ja")
    if isinstance(default, int):
        return int(raw)
    return raw


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
            errors.append(f"{key}: ungültiger Wert")
            continue
        clean[key] = value

    for net in clean.get("allowed_networks", []):
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            errors.append(f"Ungültiges Netz: {net}")
    for key in ("allowed_senders", "allowed_recipients"):
        if key in clean:
            clean[key] = [p.lower() for p in clean[key]]
            for p in clean[key]:
                if not PATTERN_RE.match(p):
                    errors.append(f"Ungültiges Adressmuster: {p}")
    if "relay_port" in clean and not 1 <= clean["relay_port"] <= 65535:
        errors.append("Relay-Port muss zwischen 1 und 65535 liegen")
    if clean.get("relay_security", "ssl") not in SECURITY_MODES:
        errors.append("Ungültiger Verschlüsselungsmodus")
    if clean.get("rewrite_name_mode", "keep") not in NAME_MODES:
        errors.append("Ungültiger Namensmodus")
    if clean.get("rewrite_enabled") and not ADDRESS_RE.match(clean.get("rewrite_from_address", "")):
        errors.append("Für das Umschreiben wird eine gültige Absenderadresse benötigt")
    if "retry_intervals" in clean:
        try:
            clean["retry_intervals"] = ", ".join(str(m) for m in parse_intervals(clean["retry_intervals"]))
        except ValueError:
            errors.append("Wiederholungsabstände: kommagetrennte Minutenwerte größer 0 angeben, z. B. 1, 5, 15, 60")
    if clean.get("notify_address") and not ADDRESS_RE.match(clean["notify_address"]):
        errors.append("Ungültige Adresse für Benachrichtigungen")
    if "queue_max_age_hours" in clean and clean["queue_max_age_hours"] < 1:
        errors.append("Maximale Verweildauer muss mindestens 1 Stunde betragen")
    for key in ("retention_days", "max_message_size_mb", "relay_timeout"):
        if key in clean and clean[key] < 0:
            errors.append(f"{key} darf nicht negativ sein")
    return clean, errors


class Settings:
    """Thread-safe settings store backed by the database."""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        stored = db.load_settings()
        data = dict(DEFAULTS)
        for key in DEFAULTS:
            if key in stored:
                data[key] = stored[key]
            elif key.upper() in os.environ:
                data[key] = _parse_env(key, os.environ[key.upper()])
        self._data = data
        db.save_settings(data)

    def get(self, key: str):
        with self._lock:
            return self._data[key]

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

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
