"""SQLite state: idempotency, sync cursor, and the shortlist timeline."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Presence of a row here means "already notified"; it is what makes a
-- redelivered Pub/Sub message or an overlapping reconcile a no-op.
CREATE TABLE IF NOT EXISTS processed (
    msg_id       TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id      TEXT NOT NULL,
    thread_id   TEXT,
    subject     TEXT,
    company     TEXT,
    round_label TEXT,
    status      TEXT NOT NULL,
    matched_on  TEXT,
    total_ids   INTEGER,
    received_at TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_company ON events(company);
-- One row per Telegram chat allowed to use the bot. Each carries its own
-- identity, so a shared watcher gives every user their own verdict rather
-- than everyone seeing the owner's.
-- Which users have already been told about which mail. Without this, one
-- failed send would re-notify everybody on the next poll.
CREATE TABLE IF NOT EXISTS delivered (
    msg_id  TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (msg_id, chat_id)
);
CREATE TABLE IF NOT EXISTS users (
    chat_id    TEXT PRIMARY KEY,
    neo_id     TEXT,
    reg_no     TEXT,
    name       TEXT,
    extra_ids  TEXT,
    is_owner   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c):
        """Additive migrations for databases created before multi-user."""
        cols = [row[1] for row in c.execute("PRAGMA table_info(events)")]
        if "chat_id" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN chat_id TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- kv ---------------------------------------------------------------
    def get(self, key, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # --- idempotency ------------------------------------------------------
    def seen(self, msg_id):
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM processed WHERE msg_id=?", (msg_id,)
            ).fetchone() is not None

    def mark_seen(self, msg_id):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO processed(msg_id,processed_at) VALUES(?,?)",
                (msg_id, _now()),
            )

    # --- timeline ---------------------------------------------------------
    def add_event(self, **kw):
        kw.setdefault("created_at", _now())
        cols = ", ".join(kw)
        marks = ", ".join("?" for _ in kw)
        with self._conn() as c:
            c.execute(
                "INSERT INTO events ({}) VALUES ({})".format(cols, marks),
                tuple(kw.values()),
            )

    def timeline(self, company=None, chat_id=None):
        sql = "SELECT * FROM events"
        clauses, args = [], []
        if company:
            clauses.append("company=?")
            args.append(company)
        if chat_id:
            clauses.append("chat_id=?")
            args.append(str(chat_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, tuple(args)).fetchall()]

    def was_delivered(self, msg_id, chat_id):
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM delivered WHERE msg_id=? AND chat_id=?",
                (msg_id, str(chat_id)),
            ).fetchone() is not None

    def mark_delivered(self, msg_id, chat_id):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO delivered(msg_id,chat_id,sent_at) "
                "VALUES(?,?,?)", (msg_id, str(chat_id), _now()))

    # --- users ------------------------------------------------------------
    def add_user(self, chat_id, is_owner=False):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO users(chat_id,is_owner,created_at) "
                "VALUES(?,?,?)",
                (str(chat_id), 1 if is_owner else 0, _now()),
            )

    def get_user(self, chat_id):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE chat_id=?", (str(chat_id),)
            ).fetchone()
        return dict(row) if row else None

    def set_user_field(self, chat_id, field, value):
        if field not in ("neo_id", "reg_no", "name", "extra_ids"):
            raise ValueError("bad field: {}".format(field))
        with self._conn() as c:
            c.execute(
                "UPDATE users SET {}=? WHERE chat_id=?".format(field),
                (value, str(chat_id)),
            )

    def list_users(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM users ORDER BY created_at").fetchall()]

    def remove_user(self, chat_id):
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM users WHERE chat_id=? AND is_owner=0", (str(chat_id),))
            return cur.rowcount
