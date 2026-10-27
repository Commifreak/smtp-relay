"""SQLite persistence for settings, SMTP sessions, transcripts and messages."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- inbound | outbound | test
    started_at  REAL NOT NULL,
    ended_at    REAL,
    peer        TEXT,
    peer_name   TEXT,
    helo        TEXT,
    result      TEXT,
    message_id  INTEGER                 -- outbound: message being relayed
);
CREATE INDEX IF NOT EXISTS ix_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS ix_sessions_message ON sessions(message_id);
CREATE TABLE IF NOT EXISTS transcript (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    ts          REAL NOT NULL,
    dir         TEXT NOT NULL,          -- C client, S server, I info, E error
    line        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_transcript_session ON transcript(session_id, id);
CREATE TABLE IF NOT EXISTS messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER,
    received_at        REAL NOT NULL,
    client_ip          TEXT,
    client_name        TEXT,
    helo               TEXT,
    mail_from          TEXT,
    rcpt_to            TEXT,            -- JSON list
    from_header        TEXT,
    to_header          TEXT,
    subject            TEXT,
    message_id_header  TEXT,
    size               INTEGER,
    status             TEXT NOT NULL,   -- rejected | relaying | queued | relayed | partial | failed
    detail             TEXT,
    relay_from         TEXT,
    relayed_at         REAL,
    raw                BLOB
);
CREATE INDEX IF NOT EXISTS ix_messages_received ON messages(received_at);
CREATE TABLE IF NOT EXISTS relays (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 1,
    senders               TEXT NOT NULL DEFAULT '[]',   -- JSON list of sender patterns
    host                  TEXT NOT NULL,
    port                  INTEGER NOT NULL,
    security              TEXT NOT NULL,                -- ssl | starttls | none
    tls_verify            INTEGER NOT NULL DEFAULT 1,
    username              TEXT NOT NULL DEFAULT '',
    password              TEXT NOT NULL DEFAULT '',
    rewrite_enabled       INTEGER NOT NULL DEFAULT 0,
    rewrite_from_address  TEXT NOT NULL DEFAULT '',
    rewrite_name_mode     TEXT NOT NULL DEFAULT 'keep',
    rewrite_fixed_name    TEXT NOT NULL DEFAULT '',
    rewrite_set_reply_to  INTEGER NOT NULL DEFAULT 1,
    rewrite_envelope      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id);
"""

# Columns added after the first release; applied to new and existing databases.
MESSAGE_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "queued_at": "REAL",               # start of the queue lifetime
    "next_attempt_at": "REAL",
    "last_attempt_at": "REAL",
    "pending_rcpts": "TEXT",           # JSON list: recipients still to deliver
    "delivered_rcpts": "TEXT",         # JSON list
    "failed_rcpts": "TEXT",            # JSON list of "rcpt: reason"
    "is_notification": "INTEGER NOT NULL DEFAULT 0",
    "relay_id": "INTEGER",              # fixed relay (test messages, notifications); NULL = route by sender
    "relay_name": "TEXT",               # relay used for the last attempt
}

MAX_LINE = 4000


class Database:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        for name, decl in MESSAGE_COLUMNS.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_queue ON messages(status, next_attempt_at)")

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def _insert(self, table: str, fields: dict) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                            tuple(fields.values())).lastrowid

    def _update(self, table: str, row_id: int, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))

    # settings

    def load_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.query("SELECT key, value FROM settings")}

    def save_settings(self, values: dict) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            for key, value in values.items():
                self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                                  (key, json.dumps(value)))
            self.conn.execute("COMMIT")

    # sessions & transcripts

    def create_session(self, kind: str, peer: str | None = None, message_id: int | None = None) -> int:
        return self._insert("sessions", {"kind": kind, "started_at": time.time(),
                                         "peer": peer, "message_id": message_id})

    def update_session(self, session_id: int, **fields) -> None:
        self._update("sessions", session_id, fields)

    def end_session(self, session_id: int, result: str | None = None) -> None:
        fields = {"ended_at": time.time()}
        if result is not None:
            fields["result"] = result
        self._update("sessions", session_id, fields)

    def add_line(self, session_id: int, direction: str, line: str) -> None:
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + f" … [{len(line) - MAX_LINE} characters truncated]"
        self.execute("INSERT INTO transcript (session_id, ts, dir, line) VALUES (?, ?, ?, ?)",
                     (session_id, time.time(), direction, line))

    def transcript(self, session_id: int) -> list[dict]:
        return self.query("SELECT ts, dir, line FROM transcript WHERE session_id = ? ORDER BY id",
                          (session_id,))

    # messages

    def insert_message(self, **fields) -> int:
        fields.setdefault("received_at", time.time())
        return self._insert("messages", fields)

    def update_message(self, message_id: int, **fields) -> None:
        self._update("messages", message_id, fields)

    # maintenance

    def purge(self, older_than: float) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM messages WHERE received_at < ? AND status NOT IN ('queued', 'relaying')",
                              (older_than,))
            self.conn.execute("DELETE FROM transcript WHERE session_id IN "
                              "(SELECT id FROM sessions WHERE started_at < ?)", (older_than,))
            self.conn.execute("DELETE FROM sessions WHERE started_at < ?", (older_than,))
            self.conn.execute("COMMIT")

    def clear_history(self) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            # Messages still waiting for delivery are kept, together with their sessions.
            self.conn.execute("DELETE FROM messages WHERE status NOT IN ('queued', 'relaying')")
            self.conn.execute("DELETE FROM sessions WHERE id NOT IN (SELECT session_id FROM messages "
                              "WHERE session_id IS NOT NULL) AND (message_id IS NULL OR message_id NOT IN "
                              "(SELECT id FROM messages))")
            self.conn.execute("DELETE FROM transcript WHERE session_id NOT IN (SELECT id FROM sessions)")
            self.conn.execute("COMMIT")
        self.execute("VACUUM")


class Transcript:
    """Writes protocol lines of one SMTP session to the database."""

    def __init__(self, db: Database, session_id: int):
        self.db = db
        self.session_id = session_id

    def client(self, line: str) -> None:
        self.db.add_line(self.session_id, "C", line)

    def server(self, line: str) -> None:
        self.db.add_line(self.session_id, "S", line)

    def info(self, line: str) -> None:
        self.db.add_line(self.session_id, "I", line)

    def error(self, line: str) -> None:
        self.db.add_line(self.session_id, "E", line)
