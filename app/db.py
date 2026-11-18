"""SQLite storage with an FTS5 full-text index. No personal data is ever stored here."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .text import STOPWORDS, fold

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    media_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    published INTEGER NOT NULL,
    fetched INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);
CREATE INDEX IF NOT EXISTS idx_articles_media ON articles(media_id, published);
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, summary, content='articles', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO articles_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TABLE IF NOT EXISTS feed_state (
    url TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_attempt INTEGER,
    last_success INTEGER,
    last_status TEXT,
    last_items INTEGER DEFAULT 0
);
"""

_TOKEN = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_TOKENS = 8


def _stem(token: str) -> str:
    """Very light Spanish plural folding so «viviendas» also finds «vivienda»."""
    if len(token) >= 5 and token.endswith("s"):
        token = token[:-1]
        if len(token) >= 6 and token.endswith("e") and token[-2] in "nrlzd":
            token = token[:-1]
    return token


def build_fts_query(query: str) -> str | None:
    """Convert free text into a safe FTS5 expression.

    Every token is wrapped in double quotes, so no user input can reach the FTS5
    query syntax (operators, column filters, NEAR, etc.).
    """
    tokens = [t for t in _TOKEN.findall(fold(query)) if len(t) > 1 and t not in STOPWORDS and not t.isdigit()]
    if not tokens:
        tokens = [t for t in _TOKEN.findall(fold(query)) if len(t) > 1]
    tokens = list(dict.fromkeys(tokens))[:MAX_QUERY_TOKENS]
    if not tokens:
        return None
    parts = [f'"{_stem(t)}"*' if len(t) >= 4 else f'"{t}"' for t in tokens]
    return " AND ".join(parts)


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- writes -----------------------------------------------------------
    def insert_articles(self, rows: Iterable[tuple[str, str, str, str, int]]) -> int:
        now = int(time.time())
        with self.connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO articles(url, media_id, title, summary, published, fetched) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(url, mid, title, summary, pub, now) for url, mid, title, summary, pub in rows],
            )
            return max(cur.rowcount, 0)

    def purge_older_than(self, days: int) -> int:
        cutoff = int(time.time()) - days * 86400
        with self.connect() as conn:
            return conn.execute("DELETE FROM articles WHERE published < ?", (cutoff,)).rowcount

    def get_feed_state(self, url: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM feed_state WHERE url = ?", (url,)).fetchone()

    def save_feed_state(self, url: str, media_id: str, *, etag: str | None, last_modified: str | None,
                        status: str, items: int, success: bool) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO feed_state(url, media_id, etag, last_modified, last_attempt, last_success,
                                          last_status, last_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     media_id=excluded.media_id,
                     etag=COALESCE(excluded.etag, feed_state.etag),
                     last_modified=COALESCE(excluded.last_modified, feed_state.last_modified),
                     last_attempt=excluded.last_attempt,
                     last_success=COALESCE(excluded.last_success, feed_state.last_success),
                     last_status=excluded.last_status,
                     last_items=excluded.last_items""",
                (url, media_id, etag, last_modified, now, now if success else None, status, items),
            )

    # --- reads ------------------------------------------------------------
    def search(self, query: str, *, since: int | None = None, limit: int = 400) -> list[sqlite3.Row]:
        fts = build_fts_query(query)
        if not fts:
            return []
        sql = (
            "SELECT a.*, bm25(articles_fts, 10.0, 2.0) AS rank FROM articles_fts "
            "JOIN articles a ON a.id = articles_fts.rowid WHERE articles_fts MATCH ?"
        )
        params: list = [fts]
        if since:
            sql += " AND a.published >= ?"
            params.append(since)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def recent(self, *, since: int, media_ids: list[str] | None = None, limit: int = 4000) -> list[sqlite3.Row]:
        sql = "SELECT * FROM articles WHERE published >= ?"
        params: list = [since]
        if media_ids:
            sql += f" AND media_id IN ({','.join('?' * len(media_ids))})"
            params.extend(media_ids)
        sql += " ORDER BY published DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def latest_for_media(self, media_id: str, limit: int = 12) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM articles WHERE media_id = ? ORDER BY published DESC LIMIT ?", (media_id, limit)
            ).fetchall()

    def stats(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT media_id) AS m, MIN(published) AS oldest, "
                "MAX(fetched) AS last FROM articles"
            ).fetchone()
            feeds = conn.execute(
                "SELECT media_id, last_status, last_success, last_items FROM feed_state"
            ).fetchall()
        return {"articles": row["n"], "media": row["m"], "oldest": row["oldest"], "last_fetch": row["last"],
                "feeds": [dict(f) for f in feeds]}
