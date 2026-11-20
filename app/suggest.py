"""Suggestions of persisted stories for what the user is typing, and «Quizá buscabas…» on the search page.

Same safety rules as the search: every term is double-quoted before it reaches FTS5 and only word characters can
appear inside the quotes. Nothing here logs or stores a query on disk; `SuggestCache` keeps results in memory for
a couple of minutes, like the search cache.
"""

from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from . import synonyms
from .db import MAX_QUERY_TOKENS, SUMMARY_WEIGHT, TITLE_WEIGHT, Database
from .synonyms import Unit
from .text import STOPWORDS, fold

MIN_CHARS, MAX_CHARS = 3, 100
WINDOW_DAYS = 30
MAX_SUGGESTIONS = 6
MIN_STORIES = 3  # with fewer stories than this, individual headlines fill the list
CANDIDATES = 400  # stories kept after the first ranking, before counting outlets
ROWS_PER_WORD = 4000
_TOKEN = re.compile(r"\w+", re.UNICODE)


def normalise(raw: str) -> str:
    """Control characters out, spaces collapsed, at most MAX_CHARS."""
    text = "".join(c if c.isprintable() else " " for c in raw)
    return " ".join(text.split())[:MAX_CHARS].strip()


@dataclass
class Plan:
    units: list[Unit]
    exprs: list[str]  # one FTS5 expression per unit


def plan(query: str, *, prefix_last: bool) -> Plan | None:
    """Units of the query with their expressions; the word being typed is a prefix when `prefix_last`."""
    query = normalise(query)
    units = synonyms.parse_units(query, expand=True, max_units=MAX_QUERY_TOKENS)
    if not units:
        return None
    words = _TOKEN.findall(fold(query))
    prefix = words[-1] if prefix_last and words and units[-1].literal[-1] == words[-1] else None
    exprs = [_expr(u, prefix if i == len(units) - 1 else None, whole=prefix_last) for i, u in enumerate(units)]
    return Plan(units, exprs)


def _word(token: str) -> str:
    """A whole word (its plural or singular too): «resc» does not find «rescate»."""
    return "(" + " OR ".join(f'"{f}"' for f in synonyms.forms(token)) + ")"


def _expr(unit: Unit, prefix: str | None, *, whole: bool) -> str:
    """`prefix` is the word being typed (prefix match); other words are whole words while typing (`whole`),
    and behave as in the normal search (prefix from 4 letters, plural folded) otherwise."""
    parts = [f'"{t}"*' if t == prefix else _word(t) if whole else synonyms.literal_term(t) for t in unit.literal]
    lit = parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"
    if not unit.synonyms:
        return lit
    atoms = [f'"{f}"' for s in unit.synonyms for f in synonyms.forms(s)]
    return "(" + " OR ".join([lit, *atoms]) + ")"


def key_words(title: str, limit: int = 4) -> str:
    """The first meaningful words of a headline, in order: what to search to find it and its siblings."""
    out: list[str] = []
    for w in _TOKEN.findall(title.lower()):
        if len(w) > 2 and not w.isdigit() and fold(w) not in STOPWORDS and w not in out:
            out.append(w)
        if len(out) == limit:
            break
    return " ".join(out)


def similar_stories(db: Database, query: str, *, prefix_last: bool, limit: int = MAX_SUGGESTIONS,
                    since: int | None = None, exclude: set[int] | None = None, require_last: bool = False,
                    ) -> list[dict]:
    """Persisted stories whose articles match the words, best first.

    A story matches a word when any of its articles does (title matches weigh ten times a summary match).
    Ranking: words matched, best bm25, outlets, recency. At least half of the words must match (and, with
    `require_last`, the word being typed). Each item: id, title, outlets, updated (timestamp of the newest
    headline), matched.
    """
    p = plan(query, prefix_last=prefix_last)
    if p is None:
        return []
    n = len(p.units)
    need = math.ceil(n / 2)
    matched: dict[int, set[int]] = {}
    best: dict[int, float] = {}
    sql = ("SELECT sa.story_id AS sid, bm25(articles_fts, ?, ?) AS rank FROM articles_fts "
           "JOIN articles a ON a.id = articles_fts.rowid JOIN story_articles sa ON sa.article_id = a.id "
           "WHERE articles_fts MATCH ? AND a.sponsored IS NULL AND a.published >= ? ORDER BY rank LIMIT ?")
    since = since if since is not None else int(time.time()) - WINDOW_DAYS * 86400
    with db.connect() as conn:
        for i, expr in enumerate(p.exprs):
            for r in conn.execute(sql, (TITLE_WEIGHT, SUMMARY_WEIGHT, expr, since, ROWS_PER_WORD)):
                matched.setdefault(r["sid"], set()).add(i)
                best[r["sid"]] = min(best.get(r["sid"], 0.0), r["rank"])
        keep = [sid for sid, m in matched.items()
                if len(m) >= need and (not require_last or n - 1 in m) and sid not in (exclude or ())]
        keep.sort(key=lambda s: (-len(matched[s]), best[s]))
        keep = keep[:CANDIDATES]
        if not keep:
            return []
        info = {}
        for start in range(0, len(keep), 400):
            chunk = keep[start:start + 400]
            marks = ",".join("?" * len(chunk))  # placeholders only; values are bound
            for r in conn.execute(
                "SELECT s.id, s.title, COUNT(DISTINCT a.media_id) AS outlets, MAX(a.published) AS newest "  # noqa: S608
                f"FROM stories s JOIN story_articles sa ON sa.story_id = s.id JOIN articles a ON a.id = sa.article_id "
                f"WHERE s.id IN ({marks}) AND a.sponsored IS NULL GROUP BY s.id", chunk):
                info[r["id"]] = r
    ranked = sorted((s for s in keep if s in info),
                    key=lambda s: (-len(matched[s]), best[s], -info[s]["outlets"], -info[s]["newest"]))
    return [{"id": s, "title": info[s]["title"], "outlets": info[s]["outlets"], "updated": info[s]["newest"],
             "matched": len(matched[s])} for s in ranked[:limit]]


def top_headlines(db: Database, query: str, *, skip_stories: list[int], limit: int,
                  since: int | None = None) -> list[dict]:
    """Best individual headlines with every word (last one a prefix) that are not part of `skip_stories`."""
    p = plan(query, prefix_last=True)
    if p is None or limit <= 0:
        return []
    since = since if since is not None else int(time.time()) - WINDOW_DAYS * 86400
    expr = " AND ".join(p.exprs)
    sql = ("SELECT a.id, a.title, a.media_id, bm25(articles_fts, ?, ?) AS rank FROM articles_fts "
           "JOIN articles a ON a.id = articles_fts.rowid "
           "WHERE articles_fts MATCH ? AND a.sponsored IS NULL AND a.published >= ? "
           "ORDER BY rank, a.published DESC LIMIT 40")
    with db.connect() as conn:
        rows = conn.execute(sql, (TITLE_WEIGHT, SUMMARY_WEIGHT, expr, since)).fetchall()
        skipped: set[int] = set()
        if skip_stories:
            marks = ",".join("?" * len(skip_stories))
            skipped = {r[0] for r in conn.execute(
                f"SELECT article_id FROM story_articles WHERE story_id IN ({marks})", skip_stories)}  # noqa: S608
    out, seen = [], set()
    for r in rows:
        key = fold(r["title"])
        if r["id"] in skipped or key in seen:
            continue
        seen.add(key)
        out.append({"title": r["title"], "media_id": r["media_id"], "keywords": key_words(r["title"])})
        if len(out) == limit:
            break
    return out


def suggest(db: Database, query: str) -> tuple[list[dict], list[dict]]:
    """(stories, headlines) for the text being typed; headlines only when there are fewer than MIN_STORIES stories."""
    query = normalise(query)
    if len(query) < MIN_CHARS:
        return [], []
    stories = similar_stories(db, query, prefix_last=True, require_last=True)
    headlines = []
    if len(stories) < MIN_STORIES:
        headlines = top_headlines(db, query, skip_stories=[s["id"] for s in stories],
                                  limit=MAX_SUGGESTIONS - len(stories))
    return stories, headlines


class SuggestCache:
    """Small in-process LRU with a time-to-live. Keys are the folded text; nothing is written to disk."""

    def __init__(self, size: int = 512, ttl: float = 120.0, clock: Callable[[], float] = time.monotonic):
        self.size, self.ttl, self.clock = size, ttl, clock
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str):
        hit = self._data.get(key)
        if hit is None:
            return None
        if self.clock() - hit[0] > self.ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return hit[1]

    def put(self, key: str, value: object) -> None:
        self._data[key] = (self.clock(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.size:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
