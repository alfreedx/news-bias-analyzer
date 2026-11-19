"""SQLite storage with an FTS5 full-text index. No personal data is ever stored here."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import sponsored
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
    fetched INTEGER NOT NULL,
    author TEXT,
    categories TEXT,
    sponsored TEXT
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

# Persistent stories: the same news event as told by several outlets. Foreign keys are not enforced
# (PRAGMA foreign_keys is off), so cascading deletes are done by hand in `purge_older_than`.
STORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    title TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_stories_updated ON stories(updated);
CREATE TABLE IF NOT EXISTS story_articles (
    story_id INTEGER NOT NULL,
    article_id INTEGER NOT NULL,
    PRIMARY KEY (story_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_story_articles_article ON story_articles(article_id);
"""

_TOKEN = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_TOKENS = 8
TITLE_WEIGHT, SUMMARY_WEIGHT = 10.0, 1.0
MIN_TITLE_MATCHES = 15  # from this many title matches on, summary-only matches are hidden by default
LEAD_CHARS = 160  # a summary-only match is kept when every query word appears this early in the summary


def in_lead(query: str, summary: str | None) -> bool:
    """True when all query words appear in the opening of the summary (the subtitle is about it).

    «Teatro de autócratas» + «Xi Jinping maneja a su antojo…» is about Xi Jinping; a crime report that
    says «…en su vivienda» at the end of its summary is not about housing.
    """
    lead = fold((summary or "")[:LEAD_CHARS])
    words = [w for w in fold(query).split() if len(w) > 1 and w not in STOPWORDS]
    return bool(words) and all(w[:5] in lead for w in words)


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


def _title_only(fts: str) -> str:
    """The same expression restricted to the title column (any token in the title counts as a title match)."""
    return "title : (" + fts.replace(" AND ", " OR ") + ")"


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self.reevaluate_sponsored(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring databases created by older versions up to the current schema (idempotent)."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        if "author" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN author TEXT")
        for column in ("categories", "sponsored"):
            if column not in columns:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT")  # noqa: S608 (fixed names)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_author ON articles(author) WHERE author IS NOT NULL")
        conn.executescript(STORY_SCHEMA)

    @staticmethod
    def reevaluate_sponsored(conn: sqlite3.Connection) -> int:
        """Apply the public rules to every stored article (idempotent; also run when the rules file changes).

        Articles stored before categories were read have none, so only their URL and headline rules can fire:
        that is documented and expected. A newly marked article leaves its persistent story at once.
        Returns how many rows changed.
        """
        changes = []
        for r in conn.execute("SELECT id, media_id, url, title, categories, sponsored FROM articles"):
            now = sponsored.match(r["media_id"], r["url"], r["title"], r["categories"])
            if now != r["sponsored"]:
                changes.append((now, r["id"]))
        if changes:
            conn.executemany("UPDATE articles SET sponsored = ? WHERE id = ?", changes)
            conn.execute("DELETE FROM story_articles WHERE article_id IN "
                         "(SELECT id FROM articles WHERE sponsored IS NOT NULL)")
            Database.prune_stories(conn)
        return len(changes)
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
    def insert_articles(self, rows: Iterable[tuple]) -> int:
        """Insert (url, media_id, title, summary, published[, author[, categories]]) rows; returns how many were new.

        Articles stored before bylines were parsed get their author filled in when the feed repeats them, and
        the sponsored mark (the outlet's own, see app/sponsored.py) is re-evaluated each time a feed repeats an
        item. A piece that is marked leaves its persistent story.
        """
        now = int(time.time())
        data = [(*r, *([None] * (7 - len(r)))) for r in map(tuple, rows)]
        marks = [sponsored.match(mid, url, title, cats) for url, mid, title, _s, _p, _a, cats in data]
        with self.connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO articles(url, media_id, title, summary, published, fetched, author, "
                "categories, sponsored) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(url, mid, title, summary, pub, now, author, cats, mark)
                 for (url, mid, title, summary, pub, author, cats), mark in zip(data, marks, strict=True)],
            )
            added = max(cur.rowcount, 0)
            conn.executemany("UPDATE articles SET author = ? WHERE url = ? AND author IS NULL",
                             [(author, url) for url, *_, author, _c in data if author])
            # re-evaluation on re-ingest: the feed's categories are the freshest evidence
            conn.executemany("UPDATE articles SET categories = ?, sponsored = ? WHERE url = ?",
                             [(cats, mark, url) for (url, *_, cats), mark in zip(data, marks, strict=True) if cats])
            conn.executemany("UPDATE articles SET sponsored = ? WHERE url = ? AND sponsored IS NOT ?",
                             [(mark, url, mark) for (url, *_, cats), mark in zip(data, marks, strict=True)
                              if not cats and mark])
            conn.execute("DELETE FROM story_articles WHERE article_id IN "
                         "(SELECT id FROM articles WHERE sponsored IS NOT NULL)")
            self.prune_stories(conn)
            return added

    def purge_older_than(self, days: int) -> int:
        cutoff = int(time.time()) - days * 86400
        with self.connect() as conn:
            removed = conn.execute("DELETE FROM articles WHERE published < ?", (cutoff,)).rowcount
            self.prune_stories(conn)
            return removed

    @staticmethod
    def prune_stories(conn: sqlite3.Connection) -> None:
        """Manual ON DELETE CASCADE: drop links to vanished articles, then stories left without articles."""
        conn.execute("DELETE FROM story_articles WHERE NOT EXISTS "
                     "(SELECT 1 FROM articles a WHERE a.id = story_articles.article_id)")
        conn.execute("DELETE FROM stories WHERE NOT EXISTS "
                     "(SELECT 1 FROM story_articles sa WHERE sa.story_id = stories.id)")

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
    def search_ex(self, query: str, *, since: int | None = None, limit: int = 400,
                  broad: bool = False) -> tuple[list[sqlite3.Row], int]:
        """Ranked matches plus how many summary-only matches were left out.

        Title matches weigh ten times a summary match (bm25 column weights; the FTS columns are title, summary).
        When at least MIN_TITLE_MATCHES headlines match in the title, results whose match is only in the RSS
        summary are dropped unless `broad` is set.
        """
        fts = build_fts_query(query)
        if not fts:
            return [], 0
        sql = (
            "SELECT a.*, bm25(articles_fts, ?, ?) AS rank FROM articles_fts "
            "JOIN articles a ON a.id = articles_fts.rowid WHERE articles_fts MATCH ?"
        )
        params: list = [TITLE_WEIGHT, SUMMARY_WEIGHT, fts]
        if since:
            sql += " AND a.published >= ?"
            params.append(since)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            if broad or len(rows) < MIN_TITLE_MATCHES:
                return rows, 0
            title_ids = {r[0] for r in conn.execute(
                "SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?", (_title_only(fts),))}
        kept = [r for r in rows if r["id"] in title_ids or in_lead(query, r["summary"])]
        if sum(r["id"] in title_ids for r in kept) < MIN_TITLE_MATCHES:
            return rows, 0
        return kept, len(rows) - len(kept)

    def search(self, query: str, *, since: int | None = None, limit: int = 400,
               broad: bool = False) -> list[sqlite3.Row]:
        return self.search_ex(query, since=since, limit=limit, broad=broad)[0]

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

    def by_author(self, author: str, *, limit: int = 200) -> list[sqlite3.Row]:
        """Headlines signed with exactly this name (as published), newest first."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM articles WHERE author = ? ORDER BY published DESC LIMIT ?", (author, limit)
            ).fetchall()

    def author_outlets(self, author: str) -> list[sqlite3.Row]:
        """Outlets where the name appears, with a headline count each."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT media_id, COUNT(*) AS n FROM articles WHERE author = ? "
                "GROUP BY media_id ORDER BY n DESC, media_id",
                (author,),
            ).fetchall()

    def get_story(self, story_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()

    def story_article_rows(self, story_id: int, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT a.* FROM story_articles sa JOIN articles a ON a.id = sa.article_id "
                "WHERE sa.story_id = ? AND a.sponsored IS NULL ORDER BY a.published DESC LIMIT ?", (story_id, limit)
            ).fetchall()

    def story_ids_for(self, article_ids: list[int]) -> dict[int, int]:
        """article id -> persisted story id, for the ids that belong to one."""
        ids = [i for i in article_ids if i is not None]
        out: dict[int, int] = {}
        with self.connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))  # placeholders only; values are bound
                for r in conn.execute(
                    f"SELECT article_id, story_id FROM story_articles WHERE article_id IN ({marks})",  # noqa: S608
                    chunk,
                ):
                    out[r["article_id"]] = r["story_id"]
        return out

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
