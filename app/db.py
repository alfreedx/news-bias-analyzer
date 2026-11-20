"""SQLite storage with an FTS5 full-text index. No personal data is ever stored here."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import sponsored, synonyms
from .synonyms import Unit
from .text import STOPWORDS, fold, stem

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
TIER = 0.1  # relevance factor per query word the article only matches through an equivalent word
MAX_STORIES = 10  # persisted stories added by the story-level match
APPROX_LIMIT = 200
_stem = stem


def in_lead(query: str, summary: str | None, *, exact: bool = False) -> bool:
    """True when all query words appear in the opening of the summary (the subtitle is about it).

    «Teatro de autócratas» + «Xi Jinping maneja a su antojo…» is about Xi Jinping; a crime report that
    says «…en su vivienda» at the end of its summary is not about housing. An equivalent word
    (data/synonyms.json) in the opening counts as the word itself unless `exact`.
    """
    lead = fold((summary or "")[:LEAD_CHARS])
    words = [w for w in fold(query).split() if len(w) > 1 and w not in STOPWORDS]
    if not words:
        return False
    syn = None if exact else synonyms.load()
    for w in words:
        if w[:5] in lead:
            continue
        related = syn.related(synonyms.key_of(w)) if syn else []
        if not any((s in lead) if " " in s else (s[:5] in lead) for s in related):
            return False
    return True


def build_fts_query(query: str, *, exact: bool = False) -> str | None:
    """Convert free text into a safe FTS5 expression.

    Every term is wrapped in double quotes, so no user input can reach the FTS5 query syntax (operators,
    column filters, NEAR, etc.). Unless `exact`, each word may also be satisfied by an equivalent word from
    data/synonyms.json: ("tok"* OR "syn1" OR "syn2" ...), still ANDed with the other words.
    """
    units = synonyms.parse_units(query, expand=not exact, max_units=MAX_QUERY_TOKENS)
    return _expr(units) if units else None


def _expr(units: list[Unit]) -> str:
    return " AND ".join(synonyms.unit_expr(u) for u in units)


def _title_only(fts: str) -> str:
    """The same expression restricted to the title column (any token in the title counts as a title match)."""
    return "title : (" + fts.replace(" AND ", " OR ") + ")"


@dataclass
class SearchResult:
    rows: list
    hidden: int = 0
    units: list[Unit] = field(default_factory=list)
    exact: bool = False
    synonyms_used: list[str] = field(default_factory=list)  # equivalent words that stood in for a typed word
    story_notes: dict[int, list[dict]] = field(default_factory=dict)  # story id -> who used which word
    approx: bool = False  # no article has every word: rows contain all but one


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
    def search_full(self, query: str, *, since: int | None = None, limit: int = 400, broad: bool = False,
                    exact: bool = False, fallback: bool = False) -> SearchResult:
        """Ranked matches with everything the page needs to explain them.

        Article level: every word (or an equivalent one, unless `exact`) must appear; title matches weigh ten
        times a summary match. An article that only has equivalent words ranks below those with the user's own
        words. When at least MIN_TITLE_MATCHES headlines match in the title, results whose match is only in the
        RSS summary are dropped unless `broad` is set.
        Story level (not `exact`, 2+ words): persisted stories whose articles together contain every word.
        Fallback (`fallback=True`): with no result at all, articles that have all words but one (`approx`).
        """
        units = synonyms.parse_units(query, expand=not exact, max_units=MAX_QUERY_TOKENS)
        out = SearchResult([], units=units, exact=exact)
        if not units:
            return out
        fts = _expr(units)
        rows, out.hidden, title_only = self._article_level(query, fts, since, limit, broad, exact)
        texts: dict[int, synonyms.Text] = {}

        def text_of(r) -> synonyms.Text:
            if r["id"] not in texts:
                texts[r["id"]] = synonyms.analyse(f"{r['title']} {r['summary']}")
            return texts[r["id"]]

        def missing(r) -> int:
            t = text_of(r)
            return sum(not synonyms.has_literal(u, t) for u in units)

        if not exact:
            for r in rows:
                r["missing"] = missing(r)
                r["rank"] *= TIER ** r["missing"]
            rows.sort(key=lambda r: (r["missing"], r["rank"]))
            if len(units) >= 2:
                added, out.story_notes = self._story_level(units, since, title_only, {r["id"] for r in rows}, text_of)
                rows = rows + added
        if fallback and not rows and len(units) >= 2:
            rows = self._approx(units, since, text_of)
            out.approx = bool(rows)
        out.rows = rows
        if not exact:
            seen: list[str] = []
            for r in rows:
                t = text_of(r)
                for u in units:
                    if u.synonyms and not synonyms.has_literal(u, t):
                        s = synonyms.matched_synonym(u, t)
                        if s and s not in seen:
                            seen.append(s)
            out.synonyms_used = seen
        return out

    def _article_level(self, query: str, fts: str, since: int | None, limit: int, broad: bool,
                       exact: bool) -> tuple[list[dict], int, bool]:
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
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            if broad or len(rows) < MIN_TITLE_MATCHES:
                return rows, 0, False
            title_ids = {r[0] for r in conn.execute(
                "SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?", (_title_only(fts),))}
        kept = [r for r in rows if r["id"] in title_ids or in_lead(query, r["summary"], exact=exact)]
        if sum(r["id"] in title_ids for r in kept) < MIN_TITLE_MATCHES:
            return rows, 0, False
        return kept, len(rows) - len(kept), True

    def _story_level(self, units: list[Unit], since: int | None, title_only: bool, have: set[int],
                     text_of) -> tuple[list[dict], dict[int, list[dict]]]:
        """Articles of persisted stories that, taken together, contain every word (or an equivalent).

        Candidates come from one FTS lookup per word (ids mapped to stories, sets intersected); each candidate is
        then checked on the plain text. A story needs 2+ articles and every word in at least one of them; those
        where a single article already has 2+ words come first. At most MAX_STORIES stories are added.
        """
        candidates: set[int] | None = None
        with self.connect() as conn:
            for u in units:
                expr = synonyms.unit_expr(u)
                if title_only:
                    expr = f"title : ({expr})"
                sql = ("SELECT DISTINCT sa.story_id FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid "
                       "JOIN story_articles sa ON sa.article_id = a.id "
                       "WHERE articles_fts MATCH ? AND a.sponsored IS NULL")
                params: list = [expr]
                if since:
                    sql += " AND a.published >= ?"
                    params.append(since)
                found = {r[0] for r in conn.execute(sql + " LIMIT 20000", params)}
                candidates = found if candidates is None else candidates & found
                if not candidates:
                    return [], {}
            ids = sorted(candidates or (), reverse=True)[:300]
            marks = ",".join("?" * len(ids))
            join = "FROM story_articles sa JOIN articles a ON a.id = sa.article_id JOIN stories s ON s.id = sa.story_id"
            sql = f"SELECT sa.story_id, s.updated AS story_updated, a.* {join} WHERE sa.story_id IN ({marks}) AND a.sponsored IS NULL"  # noqa: S608,E501
            params = list(ids)
            if since:
                sql += " AND a.published >= ?"
                params.append(since)
            members: dict[int, list[dict]] = {}
            for r in conn.execute(sql, params):
                members.setdefault(r[0], []).append(dict(r))
        scored = []
        for sid, arts in members.items():
            if len(arts) < 2:
                continue
            per_article = []
            for a in arts:
                t = text_of(a)
                a["_has"] = [synonyms.has_literal(u, t) or synonyms.matched_synonym(u, t) is not None for u in units]
                per_article.append(sum(a["_has"]))
            if not all(any(a["_has"][i] for a in arts) for i in range(len(units))):
                continue
            scored.append((-max(per_article), -arts[0]["story_updated"], sid))
        scored.sort()
        added: list[dict] = []
        notes: dict[int, list[dict]] = {}
        for neg_best, _u, sid in scored[:MAX_STORIES]:
            arts = members[sid]
            fresh = [a for a in arts if a["id"] not in have]
            if not fresh:
                continue
            for a in sorted(fresh, key=lambda a: -a["published"]):
                a["missing"] = sum(not synonyms.has_literal(u, text_of(a)) for u in units)
                a["rank"] = -(1.0 - neg_best) * TIER ** a["missing"]
                added.append(a)
                have.add(a["id"])
            notes[sid] = self._story_note(units, arts, text_of)
        return added, notes

    @staticmethod
    def _story_note(units: list[Unit], arts: list[dict], text_of) -> list[dict]:
        """Which outlets used which word: the typed one when someone did, else an equivalent."""
        note = []
        for u in units:
            literal = [a["media_id"] for a in arts if synonyms.has_literal(u, text_of(a))]
            if literal:
                note.append({"word": u.display, "equivalent_of": None, "media": list(dict.fromkeys(literal))[:2]})
                continue
            for term in u.synonyms:
                hit = [a["media_id"] for a in arts if synonyms.has_term(term, text_of(a))]
                if hit:
                    note.append({"word": term, "equivalent_of": u.display, "media": list(dict.fromkeys(hit))[:2]})
                    break
        return note

    def _approx(self, units: list[Unit], since: int | None, text_of) -> list[dict]:
        """Articles with all words but one (an equivalent word counts), best-matching first, then by bm25."""
        found: dict[int, dict] = {}
        with self.connect() as conn:
            for skip in range(len(units)):
                fts = _expr([u for i, u in enumerate(units) if i != skip])
                sql = ("SELECT a.*, bm25(articles_fts, ?, ?) AS rank FROM articles_fts "
                       "JOIN articles a ON a.id = articles_fts.rowid WHERE articles_fts MATCH ?")
                params: list = [TITLE_WEIGHT, SUMMARY_WEIGHT, fts]
                if since:
                    sql += " AND a.published >= ?"
                    params.append(since)
                for r in conn.execute(sql + " ORDER BY rank LIMIT ?", [*params, APPROX_LIMIT]):
                    found.setdefault(r["id"], dict(r))
        out = []
        for r in found.values():
            t = text_of(r)
            r["missing"] = sum(not synonyms.has_literal(u, t) for u in units)
            have = sum(synonyms.has_literal(u, t) or synonyms.matched_synonym(u, t) is not None for u in units)
            if have >= len(units) - 1:
                r["rank"] *= TIER ** (len(units) - have)
                out.append((len(units) - have, r["missing"], r["rank"], r))
        out.sort(key=lambda x: x[:3])
        return [x[3] for x in out[:APPROX_LIMIT]]

    def suggest_removals(self, query: str, *, since: int | None = None, exact: bool = False,
                         limit: int = 4) -> list[dict]:
        """For each word, the query without it and how many headlines it finds (only those that find some)."""
        units = synonyms.parse_units(query, expand=not exact, max_units=MAX_QUERY_TOKENS)
        if len(units) < 2:
            return []
        out = []
        with self.connect() as conn:
            for i, dropped in enumerate(units):
                rest = [u for j, u in enumerate(units) if j != i]
                sql = ("SELECT COUNT(*) FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid "
                       "WHERE articles_fts MATCH ? AND a.sponsored IS NULL")
                params: list = [_expr(rest)]
                if since:
                    sql += " AND a.published >= ?"
                    params.append(since)
                n = conn.execute(sql, params).fetchone()[0]
                if n:
                    out.append({"query": " ".join(u.display for u in rest), "dropped": dropped.display, "count": n})
        out.sort(key=lambda s: -s["count"])
        return out[:limit]

    def search_ex(self, query: str, *, since: int | None = None, limit: int = 400, broad: bool = False,
                  exact: bool = False) -> tuple[list[dict], int]:
        """Ranked matches plus how many summary-only matches were left out (see `search_full`)."""
        r = self.search_full(query, since=since, limit=limit, broad=broad, exact=exact)
        return r.rows, r.hidden

    def search(self, query: str, *, since: int | None = None, limit: int = 400, broad: bool = False,
               exact: bool = False) -> list[dict]:
        return self.search_ex(query, since=since, limit=limit, broad=broad, exact=exact)[0]

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
