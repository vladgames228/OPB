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

    def _lock_conn(self):
        with _lock:
            return self._conn()

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- seen ads are tracked per GROUP, not per search: if the
                -- same ad matches two searches in one group, it's sent once
                CREATE TABLE IF NOT EXISTS seen_ads (
                    group_id INTEGER NOT NULL,
                    ad_id TEXT NOT NULL,
                    seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, ad_id)
                );

                -- short-lived cache: fp -> last known ad details, so the
                -- fav/dislike/comment buttons (which only carry the fp in
                -- their callback_data) can look details back up
                CREATE TABLE IF NOT EXISTS ad_cache (
                    fp TEXT PRIMARY KEY,
                    ad_id TEXT,
                    title TEXT,
                    price TEXT,
                    url TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS favorites (
                    user_id INTEGER NOT NULL,
                    fp TEXT NOT NULL,
                    ad_id TEXT,
                    title TEXT,
                    price TEXT,
                    url TEXT,
                    saved_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, fp)
                );

                CREATE TABLE IF NOT EXISTS dislikes (
                    user_id INTEGER NOT NULL,
                    fp TEXT NOT NULL,
                    disliked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, fp)
                );

                CREATE TABLE IF NOT EXISTS comments (
                    user_id INTEGER NOT NULL,
                    fp TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, fp)
                );
            """)

    # ---------- groups ----------

    def add_group(self, user_id: int, name: str) -> int:
        with self._lock_conn() as c:
            cur = c.execute(
                "INSERT INTO groups (user_id, name) VALUES (?, ?)", (user_id, name)
            )
            return cur.lastrowid

    def list_groups(self, user_id: int):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM groups WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_group(self, group_id: int, user_id: int):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM groups WHERE id = ? AND user_id = ?", (group_id, user_id)
            ).fetchone()
            return dict(row) if row else None

    def remove_group(self, group_id: int, user_id: int) -> bool:
        with self._lock_conn() as c:
            row = c.execute(
                "SELECT id FROM groups WHERE id = ? AND user_id = ?", (group_id, user_id)
            ).fetchone()
            if not row:
                return False
            c.execute("DELETE FROM searches WHERE group_id = ?", (group_id,))
            c.execute("DELETE FROM seen_ads WHERE group_id = ?", (group_id,))
            c.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            return True

    # ---------- searches ----------

    def add_search(self, group_id: int, url: str) -> int:
        with self._lock_conn() as c:
            cur = c.execute(
                "INSERT INTO searches (group_id, url) VALUES (?, ?)", (group_id, url)
            )
            return cur.lastrowid

    def list_searches_for_user(self, user_id: int):
        """Returns all active searches for a user, joined with their group name."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT s.*, g.name AS group_name, g.id AS group_id
                FROM searches s
                JOIN groups g ON g.id = s.group_id
                WHERE g.user_id = ? AND s.active = 1
                ORDER BY g.id, s.id
                """,
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_active_searches(self):
        """Returns all active searches across all users, with group_id/user_id."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT s.id AS search_id, s.url, g.id AS group_id, g.user_id
                FROM searches s
                JOIN groups g ON g.id = s.group_id
                WHERE s.active = 1
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def remove_search(self, search_id: int, user_id: int) -> bool:
        with self._lock_conn() as c:
            row = c.execute(
                """
                SELECT s.id FROM searches s
                JOIN groups g ON g.id = s.group_id
                WHERE s.id = ? AND g.user_id = ?
                """,
                (search_id, user_id),
            ).fetchone()
            if not row:
                return False
            c.execute("UPDATE searches SET active = 0 WHERE id = ?", (search_id,))
            return True

    # ---------- seen ads (per group) ----------

    def is_seen(self, group_id: int, ad_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM seen_ads WHERE group_id = ? AND ad_id = ?",
                (group_id, ad_id),
            ).fetchone()
            return row is not None

    def mark_seen(self, group_id: int, ad_id: str):
        with self._lock_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO seen_ads (group_id, ad_id) VALUES (?, ?)",
                (group_id, ad_id),
            )

    def count_seen(self, group_id: int) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM seen_ads WHERE group_id = ?", (group_id,)
            ).fetchone()
            return row["n"]

    # ---------- ad cache (for callback buttons) ----------

    def cache_ad(self, fp: str, ad_id: str, title: str, price: str, url: str):
        with self._lock_conn() as c:
            c.execute(
                """
                INSERT INTO ad_cache (fp, ad_id, title, price, url, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(fp) DO UPDATE SET
                    ad_id=excluded.ad_id, title=excluded.title,
                    price=excluded.price, url=excluded.url,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (fp, ad_id, title, price, url),
            )

    def get_cached_ad(self, fp: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM ad_cache WHERE fp = ?", (fp,)).fetchone()
            return dict(row) if row else None

    # ---------- favorites ----------

    def add_favorite(self, user_id: int, fp: str, ad_id: str, title: str, price: str, url: str):
        with self._lock_conn() as c:
            c.execute(
                """
                INSERT INTO favorites (user_id, fp, ad_id, title, price, url)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, fp) DO UPDATE SET
                    ad_id=excluded.ad_id, title=excluded.title,
                    price=excluded.price, url=excluded.url
                """,
                (user_id, fp, ad_id, title, price, url),
            )

    def list_favorites(self, user_id: int):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM favorites WHERE user_id = ? ORDER BY saved_at DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- dislikes ----------

    def add_dislike(self, user_id: int, fp: str):
        with self._lock_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO dislikes (user_id, fp) VALUES (?, ?)",
                (user_id, fp),
            )

    def is_disliked(self, user_id: int, fp: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM dislikes WHERE user_id = ? AND fp = ?", (user_id, fp)
            ).fetchone()
            return row is not None

    # ---------- comments ----------

    def upsert_comment(self, user_id: int, fp: str, comment: str):
        with self._lock_conn() as c:
            c.execute(
                """
                INSERT INTO comments (user_id, fp, comment, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, fp) DO UPDATE SET
                    comment=excluded.comment, updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, fp, comment),
            )

    def get_comment(self, user_id: int, fp: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT comment FROM comments WHERE user_id = ? AND fp = ?",
                (user_id, fp),
            ).fetchone()
            return row["comment"] if row else None
