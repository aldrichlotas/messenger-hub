"""SQLite store (data/hub.db): every message as an item, plus what the rules derive from them.

    item     one message (or other raw unit) from a synced source
    entry    something to act on: deadline | meeting | task | capture | win
    mention  an item that mentions you, your role or your committees
    sent     what the bot already sent (dedupes briefs and alerts)
    meta     small key/value state (ingest bookmarks...)
"""

import datetime as dt
import os
import sqlite3
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "hub.db")
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS item (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,            -- messenger | telegram-bot | app
    channel TEXT NOT NULL,           -- chat slug, "self", "bot", "app"
    ext_id TEXT NOT NULL,            -- id inside the channel
    org TEXT,
    chat_name TEXT,
    author TEXT,
    is_me INTEGER DEFAULT 0,
    ts TEXT,
    text TEXT,
    ref TEXT,                        -- "<chat>#<id>"
    UNIQUE (source, channel, ext_id)
);
CREATE TABLE IF NOT EXISTS entry (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- deadline | meeting | task | capture | win
    title TEXT NOT NULL,
    due TEXT,                        -- ISO datetime, or NULL
    due_has_time INTEGER DEFAULT 0,
    org TEXT,
    item_id INTEGER,
    ref TEXT,
    status TEXT DEFAULT 'open',      -- open | done | dismissed | past
    review TEXT DEFAULT 'unreviewed',-- unreviewed | accepted | rejected
    origin TEXT,                     -- rule | capture | bot | app
    confidence REAL DEFAULT 0,
    reasons TEXT,
    created TEXT,
    updated TEXT,
    UNIQUE (item_id, kind)
);
CREATE TABLE IF NOT EXISTS mention (
    id INTEGER PRIMARY KEY,
    item_id INTEGER UNIQUE,
    org TEXT,
    strength INTEGER,                -- 3 you / your title, 2 your committee, 1 everyone
    reason TEXT,
    read INTEGER DEFAULT 0,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS entry_due ON entry (status, due);
CREATE INDEX IF NOT EXISTS item_channel ON item (channel, ts);
"""


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with _lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def q(self, sql: str, args=()) -> list:
        with _lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args=()) -> int:
        with _lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # meta
    def get_meta(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        self.run("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    # items
    def add_item(self, **f) -> tuple:
        """Insert an item; returns (id, created). Existing items are left alone."""
        with _lock:
            row = self.db.execute(
                "SELECT id FROM item WHERE source = ? AND channel = ? AND ext_id = ?", (f["source"], f["channel"], str(f["ext_id"]))
            ).fetchone()
            if row:
                return row["id"], False
            cur = self.db.execute(
                "INSERT INTO item (source, channel, ext_id, org, chat_name, author, is_me, ts, text, ref)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f["source"], f["channel"], str(f["ext_id"]), f.get("org"), f.get("chat_name"), f.get("author"),
                    1 if f.get("is_me") else 0, f.get("ts"), f.get("text"), f.get("ref"),
                ),
            )
            self.db.commit()
            return cur.lastrowid, True

    # entries
    def add_entry(self, **f) -> int:
        stamp = now_iso()
        with _lock:
            if f.get("item_id"):
                row = self.db.execute("SELECT id FROM entry WHERE item_id = ? AND kind = ?", (f["item_id"], f["kind"])).fetchone()
                if row:
                    return row["id"]
            cur = self.db.execute(
                "INSERT INTO entry (kind, title, due, due_has_time, org, item_id, ref, status, review, origin, confidence, reasons, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f["kind"], f["title"], f.get("due"), 1 if f.get("due_has_time") else 0, f.get("org"), f.get("item_id"),
                    f.get("ref"), f.get("status", "open"), f.get("review", "unreviewed"), f.get("origin", "rule"),
                    f.get("confidence", 0), f.get("reasons"), stamp, stamp,
                ),
            )
            self.db.commit()
            return cur.lastrowid

    def update_entry(self, entry_id: int, **f) -> None:
        allowed = {"title", "due", "due_has_time", "org", "status", "review", "kind"}
        sets = {k: v for k, v in f.items() if k in allowed}
        if not sets:
            return
        sets["updated"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.run(f"UPDATE entry SET {cols} WHERE id = ?", (*sets.values(), entry_id))

    def add_mention(self, item_id: int, org, strength: int, reason: str, ts) -> None:
        self.run(
            "INSERT OR IGNORE INTO mention (item_id, org, strength, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (item_id, org, strength, reason, ts),
        )

    # sent log
    def was_sent(self, key: str) -> bool:
        return self.one("SELECT 1 AS x FROM sent WHERE key = ?", (key,)) is not None

    def mark_sent(self, key: str) -> None:
        self.run("INSERT OR IGNORE INTO sent (key, at) VALUES (?, ?)", (key, now_iso()))

    def sent_today(self, prefix: str) -> int:
        today = dt.date.today().isoformat()
        row = self.one("SELECT COUNT(*) AS n FROM sent WHERE key LIKE ? AND at >= ?", (prefix + "%", today))
        return row["n"] if row else 0
