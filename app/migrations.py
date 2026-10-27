"""Versioned database migrations.

Every schema or data change for existing installations is a numbered entry in MIGRATIONS.
Migrations must be idempotent (existing databases may already contain the change) and return
a short result text. Pending migrations are announced and reported in a block on stdout when
the container starts; a backup of the database is taken first.
"""
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from .db import Database
from .relays import Relays

LINE = "=" * 64


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    id: int
    name: str
    run: Callable[[Database], str]


def _add_columns(db: Database, table: str, columns: dict) -> str:
    existing = {r["name"] for r in db.query(f"PRAGMA table_info({table})")}
    missing = {name: decl for name, decl in columns.items() if name not in existing}
    for name, decl in missing.items():
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    return f"added {len(missing)} column(s)" if missing else "already up to date"


QUEUE_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "queued_at": "REAL",               # start of the queue lifetime
    "next_attempt_at": "REAL",
    "last_attempt_at": "REAL",
    "pending_rcpts": "TEXT",           # JSON list: recipients still to deliver
    "delivered_rcpts": "TEXT",         # JSON list
    "failed_rcpts": "TEXT",            # JSON list of "rcpt: reason"
    "is_notification": "INTEGER NOT NULL DEFAULT 0",
}
RELAY_COLUMNS = {
    "relay_id": "INTEGER",             # fixed relay (test messages, notifications); NULL = route by sender
    "relay_name": "TEXT",              # relay used for the last attempt
}


def _queue_columns(db: Database) -> str:
    result = _add_columns(db, "messages", QUEUE_COLUMNS)
    db.execute("CREATE INDEX IF NOT EXISTS ix_messages_queue ON messages(status, next_attempt_at)")
    return result


def _single_relay(db: Database) -> str:
    relays = Relays(db)
    if not relays.migrate_single_relay():
        return "nothing to migrate"
    return f"created relay “{relays.first()['name']}” from the previous relay settings"


def _alert_address(db: Database) -> str:
    stored = db.load_settings()
    if "notify_address" not in stored:
        return "nothing to migrate"
    address = stored["notify_address"]
    if address and "alert_address" not in stored:
        db.execute("INSERT INTO settings (key, value) VALUES ('alert_address', ?)", (json.dumps(address),))
    db.execute("DELETE FROM settings WHERE key = 'notify_address'")
    return f"alerts go to {address}" if address else "removed the empty notification address"


MIGRATIONS = [
    Migration(1, "Add delivery queue columns to messages (1.1)", _queue_columns),
    Migration(2, "Add relay columns to messages (1.3)", lambda db: _add_columns(db, "messages", RELAY_COLUMNS)),
    Migration(3, "Move the single relay configuration into relays (1.3)", _single_relay),
    Migration(4, "Use the failure notification address for alerts (1.4)", _alert_address),
]


def schema_version(db: Database) -> int:
    row = db.query_one("SELECT MAX(id) AS v FROM schema_migrations")
    return row["v"] or 0


def backup(db: Database, data_dir: str) -> str:
    os.makedirs(os.path.join(data_dir, "backups"), exist_ok=True)
    path = os.path.join(data_dir, "backups",
                        f"smtp-relay-{time.strftime('%Y%m%d-%H%M%S')}-schema-{schema_version(db)}.db")
    target = sqlite3.connect(path)
    with db.lock:
        db.conn.backup(target)
    target.close()
    return path


def migrate(db: Database, data_dir: str | None = None, out: Callable[[str], None] | None = None,
            notes: list[str] | None = None) -> int:
    """Apply pending migrations. With `out`, announce and report them as a block (not for new databases).

    Returns the number of applied migrations; raises MigrationError if one fails.
    """
    applied = {r["id"] for r in db.query("SELECT id FROM schema_migrations")}
    pending = [m for m in MIGRATIONS if m.id not in applied]
    notes = notes or []
    show = out is not None and (notes or (pending and not db.created))
    if show:
        out(LINE)
        out("  smtp-relay – database migrations")
        for note in notes:
            out(f"  {note}")
        if pending:
            out(f"  Pending migrations: {len(pending)}")
            for m in pending:
                out(f"    [{m.id}] {m.name}")
            if data_dir:
                out(f"  Backup: {backup(db, data_dir)}")
            out("")
    for m in pending:
        with db.lock:
            db.conn.execute("BEGIN")
            try:
                result = m.run(db)
                db.conn.execute("INSERT INTO schema_migrations (id, name, applied_at) VALUES (?, ?, ?)",
                                (m.id, m.name, time.time()))
                db.conn.execute("COMMIT")
            except Exception as exc:
                db.conn.execute("ROLLBACK")
                if show:
                    out(f"  [{m.id}] {m.name}: FAILED – {type(exc).__name__}: {exc}")
                    out("  The failed migration was rolled back; smtp-relay will not start.")
                    out(LINE)
                raise MigrationError(f"Migration {m.id} failed: {exc}") from exc
        if show:
            out(f"  [{m.id}] {m.name}: done – {result}")
    if show:
        if pending:
            out(f"  {len(pending)} migration(s) applied, schema version {schema_version(db)}.")
        out(LINE)
    return len(pending)


def open_database(path: str) -> Database:
    """Open a database and bring its schema up to date without any output (tests, tools)."""
    db = Database(path)
    migrate(db)
    return db
