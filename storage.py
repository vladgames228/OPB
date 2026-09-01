import sqlite3
import threading
from contextlib import contextmanager

_lock = threading.Lock()


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    active INTEGER DEFAULT 1
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS seen_ads (
                    search_id INTEGER NOT NULL,
                    ad_id TEXT NOT NULL,
                    seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (search_id, ad_id)
                )
            """)

    def add_search(self, user_id: int, url: str, title: str = None) -> int:
        with self._lock_conn() as c:
            cur = c.execute(
                "INSERT INTO searches (user_id, url, title) VALUES (?, ?, ?)",
                (user_id, url, title),
            )
            return cur.lastrowid

    def _lock_conn(self):
        with _lock:
            return self._conn()

    def list_searches(self, user_id: int):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM searches WHERE user_id = ? AND active = 1 ORDER BY id",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_active_searches(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM searches WHERE active = 1").fetchall()
            return [dict(r) for r in rows]

    def remove_search(self, search_id: int, user_id: int) -> bool:
        with self._lock_conn() as c:
            cur = c.execute(
                "UPDATE searches SET active = 0 WHERE id = ? AND user_id = ?",
                (search_id, user_id),
            )
            return cur.rowcount > 0

    def is_seen(self, search_id: int, ad_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM seen_ads WHERE search_id = ? AND ad_id = ?",
                (search_id, ad_id),
            ).fetchone()
            return row is not None

    def mark_seen(self, search_id: int, ad_id: str):
        with self._lock_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO seen_ads (search_id, ad_id) VALUES (?, ?)",
                (search_id, ad_id),
            )

    def count_seen(self, search_id: int) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM seen_ads WHERE search_id = ?",
                (search_id,),
            ).fetchone()
            return row["n"]
