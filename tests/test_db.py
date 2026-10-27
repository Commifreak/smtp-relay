import sqlite3

from app.db import MESSAGE_COLUMNS, Database


def test_migrates_first_release_schema(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, "
                 "received_at REAL NOT NULL, mail_from TEXT, status TEXT NOT NULL, raw BLOB)")
    conn.execute("INSERT INTO messages (received_at, mail_from, status) VALUES (1, 'a@b.de', 'deferred')")
    conn.commit()
    conn.close()

    db = Database(path)
    columns = {r["name"] for r in db.query("PRAGMA table_info(messages)")}
    assert set(MESSAGE_COLUMNS) <= columns
    row = db.query_one("SELECT * FROM messages")
    assert row["status"] == "deferred" and row["attempts"] == 0


def test_cleanup_keeps_queued_messages(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.insert_message(received_at=1, status="relayed")
    queued = db.insert_message(received_at=1, status="queued")
    db.purge(older_than=100)
    assert [r["id"] for r in db.query("SELECT id FROM messages")] == [queued]
    db.insert_message(status="failed")
    db.clear_history()
    assert [r["id"] for r in db.query("SELECT id FROM messages")] == [queued]
