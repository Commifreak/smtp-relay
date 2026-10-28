import json
import os
import sqlite3

import pytest

from app import migrations
from app.db import Database
from app.migrations import QUEUE_COLUMNS, RELAY_COLUMNS, Migration, MigrationError, migrate, open_database


def old_database(path):
    """A database as written by the first release, with the single-relay settings of 1.2."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, "
                 "received_at REAL NOT NULL, mail_from TEXT, status TEXT NOT NULL, raw BLOB)")
    conn.execute("INSERT INTO messages (received_at, mail_from, status) VALUES (1, 'a@b.de', 'deferred')")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for key, value in {"relay_host": "smtp.mailbox.org", "allowed_senders": ["nas@example.com"]}.items():
        conn.execute("INSERT INTO settings VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()


def test_migrates_old_database_with_report(tmp_path):
    path = str(tmp_path / "old.db")
    old_database(path)
    db = Database(path)
    lines = []
    assert migrate(db, str(tmp_path), out=lines.append) == len(migrations.MIGRATIONS)

    output = "\n".join(lines)
    assert "smtp-relay – database migrations" in output
    assert "[1] Add delivery queue columns to messages (1.1): done – added 8 column(s)" in output
    assert "[3] Move the single relay configuration into relays (1.3): done – created relay" in output
    assert f"schema version {migrations.MIGRATIONS[-1].id}." in output
    backups = os.listdir(tmp_path / "backups")
    assert len(backups) == 1 and backups[0].endswith("-schema-0.db")

    columns = {r["name"] for r in db.query("PRAGMA table_info(messages)")}
    assert set(QUEUE_COLUMNS) | set(RELAY_COLUMNS) <= columns
    row = db.query_one("SELECT * FROM messages")
    assert row["status"] == "deferred" and row["attempts"] == 0
    assert db.query_one("SELECT name, senders, port, security FROM relays") == {
        "name": "smtp.mailbox.org", "senders": '["nas@example.com"]', "port": 465, "security": "ssl"}

    lines.clear()
    assert migrate(Database(path), str(tmp_path), out=lines.append) == 0  # next start: nothing pending, no block
    assert lines == []


def test_notification_address_becomes_alert_address(tmp_path):
    path = str(tmp_path / "t.db")
    db = open_database(path)
    db.execute("DELETE FROM schema_migrations WHERE id = 4")
    db.save_settings({"notify_address": "admin@example.com"})
    lines = []
    migrate(Database(path), out=lines.append)
    assert "  [4] Use the failure notification address for alerts (1.4): done – alerts go to admin@example.com" in lines
    stored = Database(path).load_settings()
    assert stored["alert_address"] == "admin@example.com" and "notify_address" not in stored


def test_already_migrated_database_reports_up_to_date(tmp_path):
    """Databases of 1.3.0 already contain the changes but no migration records."""
    path = str(tmp_path / "t.db")
    open_database(path).execute("DELETE FROM schema_migrations")
    lines = []
    migrate(Database(path), out=lines.append)
    assert "  [1] Add delivery queue columns to messages (1.1): done – already up to date" in lines
    assert "  [3] Move the single relay configuration into relays (1.3): done – nothing to migrate" in lines


def test_new_database_is_migrated_quietly(tmp_path):
    db = Database(str(tmp_path / "new.db"))
    lines = []
    assert migrate(db, str(tmp_path), out=lines.append) == len(migrations.MIGRATIONS)
    assert lines == [] and not (tmp_path / "backups").exists()
    assert migrations.schema_version(db) == migrations.MIGRATIONS[-1].id


def test_notes_are_shown_without_pending_migrations(tmp_path):
    path = str(tmp_path / "t.db")
    open_database(path)
    lines = []
    migrate(Database(path), out=lines.append, notes=["Renamed mta-relay.db to smtp-relay.db"])
    assert "  Renamed mta-relay.db to smtp-relay.db" in lines and "Pending migrations" not in "\n".join(lines)


def test_failed_migration_is_rolled_back(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    open_database(path)

    def broken(db):
        db.execute("ALTER TABLE messages ADD COLUMN half_done TEXT")
        raise RuntimeError("boom")

    last = migrations.MIGRATIONS[-1].id
    monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, Migration(99, "Broken", broken)])
    db = Database(path)
    lines = []
    with pytest.raises(MigrationError):
        migrate(db, str(tmp_path), out=lines.append)
    assert any("[99] Broken: FAILED – RuntimeError: boom" in line for line in lines)
    assert "half_done" not in {r["name"] for r in db.query("PRAGMA table_info(messages)")}
    assert migrations.schema_version(db) == last


def test_migration_ids_are_unique_and_ascending():
    ids = [m.id for m in migrations.MIGRATIONS]
    assert ids == sorted(set(ids))


def test_cleanup_keeps_queued_messages(tmp_path):
    db = open_database(str(tmp_path / "t.db"))
    db.insert_message(received_at=1, status="relayed")
    queued = db.insert_message(received_at=1, status="queued")
    db.purge(older_than=100)
    assert [r["id"] for r in db.query("SELECT id FROM messages")] == [queued]
    db.insert_message(status="failed")
    db.clear_history()
    assert [r["id"] for r in db.query("SELECT id FROM messages")] == [queued]
