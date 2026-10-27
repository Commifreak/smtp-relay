"""Relay servers and sender-based routing.

Each relay has a list of sender patterns. A sender may only use smtp-relay if it matches a
pattern of an enabled relay; the most specific match wins (exact address > domain > '*').
"""
import json

from .config import ADDRESS_RE, NAME_MODES, PATTERN_RE, SECURITY_MODES, split_list
from .db import Database

BOOL_FIELDS = ("enabled", "tls_verify", "rewrite_enabled", "rewrite_set_reply_to", "rewrite_envelope")
FIELDS = ("name", "enabled", "senders", "host", "port", "security", "tls_verify", "username", "password",
          "rewrite_enabled", "rewrite_from_address", "rewrite_name_mode", "rewrite_fixed_name",
          "rewrite_set_reply_to", "rewrite_envelope")
# A new relay starts without server details; they have to be entered explicitly.
NEW_RELAY = {"name": "", "enabled": True, "senders": [], "host": "", "port": "",
             "security": "", "tls_verify": True, "username": "", "password": "", "rewrite_enabled": False,
             "rewrite_from_address": "", "rewrite_name_mode": "keep", "rewrite_fixed_name": "",
             "rewrite_set_reply_to": True, "rewrite_envelope": True}


def specificity(pattern: str) -> int:
    if pattern == "*":
        return 0
    if pattern.startswith(("@", "*@")):
        return 1
    return 2


def pattern_matches(address: str, pattern: str) -> bool:
    address = address.strip().lower()
    if pattern == "*":
        return True
    if pattern.startswith(("@", "*@")):
        return "@" in address and address.rsplit("@", 1)[1] == pattern.split("@", 1)[1]
    return address == pattern


def as_settings(relay: dict) -> dict:
    """Map a relay to the setting keys used by the transport and header rewriting."""
    return {"relay_host": relay["host"], "relay_port": relay["port"], "relay_security": relay["security"],
            "relay_tls_verify": relay["tls_verify"], "relay_username": relay["username"],
            "relay_password": relay["password"],
            **{k: relay[k] for k in FIELDS if k.startswith("rewrite_")}}


class Relays:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _decode(row: dict) -> dict:
        row["senders"] = json.loads(row["senders"] or "[]")
        for key in BOOL_FIELDS:
            row[key] = bool(row[key])
        return row

    def all(self) -> list[dict]:
        return [self._decode(r) for r in self.db.query("SELECT * FROM relays ORDER BY id")]

    def get(self, relay_id: int | None) -> dict | None:
        if relay_id is None:
            return None
        row = self.db.query_one("SELECT * FROM relays WHERE id = ?", (relay_id,))
        return self._decode(row) if row else None

    def first(self) -> dict | None:
        relays = self.all()
        return relays[0] if relays else None

    def route(self, address: str) -> tuple[dict | None, str | None]:
        """Relay for a sender address and the pattern that matched, or (None, None)."""
        best = None
        for relay in self.all():
            if not relay["enabled"]:
                continue
            for pattern in relay["senders"]:
                if pattern_matches(address, pattern):
                    rank = specificity(pattern)
                    if best is None or rank > best[0]:
                        best = (rank, relay, pattern)
        return (best[1], best[2]) if best else (None, None)

    def validate(self, values: dict, relay_id: int | None = None,
                 require_senders: bool = True) -> tuple[dict, list[str]]:
        current = self.get(relay_id) or NEW_RELAY
        clean = {**current, **{k: v for k, v in values.items() if k in FIELDS}}
        clean.pop("id", None)
        errors = []
        for key in BOOL_FIELDS:
            v = clean[key]
            clean[key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
        clean["senders"] = [p.lower() for p in split_list(clean["senders"])]
        for key in ("name", "host", "username", "rewrite_from_address", "rewrite_fixed_name"):
            clean[key] = str(clean[key]).strip()
        if relay_id is not None and not values.get("password"):
            clean["password"] = current["password"]  # empty field keeps the stored password
        if not clean["name"]:
            errors.append("Name is required")
        if not clean["host"]:
            errors.append("Host is required")
        try:
            clean["port"] = int(clean["port"])
            if not 1 <= clean["port"] <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Port must be between 1 and 65535")
        if clean["security"] not in SECURITY_MODES:
            errors.append("Choose an encryption mode")
        if clean["rewrite_name_mode"] not in NAME_MODES:
            errors.append("Invalid display name mode")
        if clean["rewrite_enabled"] and not ADDRESS_RE.match(clean["rewrite_from_address"]):
            errors.append("Rewriting requires a valid sender address")
        if require_senders and not clean["senders"]:
            errors.append("Enter at least one sender address or domain")
        for p in clean["senders"]:
            if not PATTERN_RE.match(p):
                errors.append(f"Invalid sender pattern: {p}")
        if clean["enabled"]:
            for other in self.all():
                if other["id"] == relay_id or not other["enabled"]:
                    continue
                for p in set(clean["senders"]) & set(other["senders"]):
                    errors.append(f"Sender pattern {p} is already used by relay “{other['name']}”")
        return clean, errors

    def save(self, values: dict, relay_id: int | None = None,
             require_senders: bool = True) -> tuple[int | None, list[str]]:
        clean, errors = self.validate(values, relay_id, require_senders)
        if errors:
            return None, errors
        row = {k: (json.dumps(clean[k]) if k == "senders" else clean[k]) for k in FIELDS}
        if relay_id is None:
            cols = ", ".join(row)
            relay_id = self.db.execute(f"INSERT INTO relays ({cols}) VALUES ({', '.join('?' for _ in row)})",
                                       tuple(row.values())).lastrowid
        else:
            sets = ", ".join(f"{k} = ?" for k in row)
            self.db.execute(f"UPDATE relays SET {sets} WHERE id = ?", (*row.values(), relay_id))
        return relay_id, []

    def delete(self, relay_id: int) -> None:
        self.db.execute("DELETE FROM relays WHERE id = ?", (relay_id,))

    def migrate_single_relay(self) -> bool:
        """Turn the relay settings of releases before 1.3 into the first relay."""
        stored = self.db.load_settings()
        if self.db.query_one("SELECT id FROM relays LIMIT 1") or not stored.get("relay_host"):
            return False
        # 465/ssl were the defaults of releases before 1.3
        relay = {**NEW_RELAY, "name": stored["relay_host"], "senders": stored.get("allowed_senders", []),
                 "host": stored["relay_host"], "port": 465, "security": "ssl"}
        legacy = {"port": "relay_port", "security": "relay_security", "tls_verify": "relay_tls_verify",
                  "username": "relay_username", "password": "relay_password"}
        for field, key in legacy.items():
            if key in stored:
                relay[field] = stored[key]
        for field in FIELDS:
            if field.startswith("rewrite_") and field in stored:
                relay[field] = stored[field]
        row = {k: (json.dumps(relay[k]) if k == "senders" else relay[k]) for k in FIELDS}
        self.db.execute(f"INSERT INTO relays ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                        tuple(row.values()))
        old_keys = ["allowed_senders", *legacy.values(), "relay_host",
                    *(f for f in FIELDS if f.startswith("rewrite_"))]
        self.db.execute(f"DELETE FROM settings WHERE key IN ({', '.join('?' for _ in old_keys)})", old_keys)
        return True
