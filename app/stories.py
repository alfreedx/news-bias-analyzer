"""Persistent stories: incremental assignment of new headlines to stories that outlive one request.

The similarity rule is exactly the one used by `analysis.cluster` (keyword overlap coefficient); a story is
matched through the keywords of its most central headline.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict

from .analysis import Article, best_match, cluster, make_article
from .catalog import Catalog
from .db import Database

log = logging.getLogger("newsbias.stories")

WINDOW = 72 * 3600  # articles and stories older than this are not (re)assigned
CENTRAL_SAMPLE = 60  # the central headline is looked for among the most recent members only


def central_article(articles: list[Article]) -> Article:
    """The headline sharing the most keywords with all the others (ties: the earliest published)."""
    sample = sorted(articles, key=lambda a: (-a.published, a.id or 0))[:CENTRAL_SAMPLE]
    if len(sample) == 1:
        return sample[0]

    def weight(a: Article) -> tuple[int, int]:
        return (sum(len(a.keys & b.keys) for b in sample if b is not a), -a.published)

    return max(sample, key=weight)


def _articles(catalog: Catalog, rows) -> list[Article]:
    return [make_article(catalog, title=r["title"], url=r["url"], summary=r["summary"], published=r["published"],
                         media_id=r["media_id"], author=r["author"], article_id=r["id"]) for r in rows]


def assign_stories(db: Database, catalog: Catalog, now: int | None = None) -> dict[str, int]:
    """Attach unassigned recent headlines to stories, creating a story when >= 2 of them match each other.

    Runs in one write transaction on its own connection, so it is safe to call from a worker thread while
    requests keep reading (WAL). Returns counters for logging and tests.
    """
    now = now or int(time.time())
    since = now - WINDOW
    news_ids = {m.id for m in catalog.news_media}
    attached = created = 0
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT a.* FROM articles a WHERE a.published >= ? AND NOT EXISTS "
            "(SELECT 1 FROM story_articles sa WHERE sa.article_id = a.id) ORDER BY a.published", (since,)
        ).fetchall()
        fresh = _articles(catalog, [r for r in rows if r["media_id"] in news_ids])
        if not fresh:
            return {"created": 0, "attached": 0, "pending": 0}

        story_rows = conn.execute("SELECT id, keywords FROM stories WHERE updated >= ?", (since,)).fetchall()
        ids = [r["id"] for r in story_rows]
        seeds = [set(r["keywords"].split()) for r in story_rows]
        index: dict[str, list[int]] = defaultdict(list)
        for i, keys in enumerate(seeds):
            for k in keys:
                index[k].append(i)

        links: list[tuple[int, int]] = []
        touched: set[int] = set()
        leftover: list[Article] = []
        for art in fresh:
            hit = best_match(art.keys, seeds, index)
            if hit is None:
                leftover.append(art)
            else:
                links.append((ids[hit], art.id))
                touched.add(ids[hit])
                attached += 1

        for story in cluster(leftover):
            members = story.articles
            if len(members) < 2:
                continue
            lead = central_article(members)
            cur = conn.execute(
                "INSERT INTO stories(created, updated, title, keywords) VALUES (?, ?, ?, ?)",
                (now, max(a.published for a in members), lead.title, " ".join(sorted(lead.keys))),
            )
            links.extend((cur.lastrowid, a.id) for a in members)
            ids.append(cur.lastrowid)
            seeds.append(set(lead.keys))
            for k in lead.keys:
                index[k].append(len(seeds) - 1)
            created += 1

        # cluster() keeps one headline per outlet: attach the rest to the stories just created
        linked = {article_id for _, article_id in links}
        for art in leftover:
            if art.id in linked:
                continue
            hit = best_match(art.keys, seeds, index)
            if hit is not None:
                links.append((ids[hit], art.id))
                touched.add(ids[hit])
                attached += 1
                linked.add(art.id)

        pending = sum(a.id not in linked for a in leftover)
        conn.executemany("INSERT OR IGNORE INTO story_articles(story_id, article_id) VALUES (?, ?)", links)
        _refresh(conn, catalog, sorted(touched))
    log.info("stories: %d created, %d headlines attached", created, attached)
    return {"created": created, "attached": attached, "pending": pending}


def _refresh(conn, catalog: Catalog, story_ids: list[int]) -> None:
    """Recompute title, keywords and `updated` of stories whose membership changed."""
    for start in range(0, len(story_ids), 400):
        chunk = story_ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        by_story: dict[int, list] = defaultdict(list)
        join = "FROM story_articles sa JOIN articles a ON a.id = sa.article_id"
        sql = f"SELECT sa.story_id AS story_id, a.* {join} WHERE sa.story_id IN ({marks})"  # noqa: S608
        for r in conn.execute(sql, chunk):
            by_story[r["story_id"]].append(r)
        for sid, member_rows in by_story.items():
            members = _articles(catalog, member_rows)
            lead = central_article(members)
            conn.execute("UPDATE stories SET title = ?, keywords = ?, updated = ? WHERE id = ?",
                         (lead.title, " ".join(sorted(lead.keys)), max(a.published for a in members), sid))


def majority_story(articles: list[Article], story_of: dict[int, int]) -> int | None:
    """Persisted story that holds at least half of a clustered story's headlines (and at least two)."""
    counts = Counter(story_of[a.id] for a in articles if a.id in story_of)
    if not counts:
        return None
    sid, n = counts.most_common(1)[0]
    return sid if n >= 2 and n * 2 >= len(articles) else None
