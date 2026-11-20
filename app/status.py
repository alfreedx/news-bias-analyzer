"""Public health of the feed collection (page /estado). Only catalog and index facts: no IPs, no user data."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .catalog import Catalog
from .db import FEED_FAILURE_LIMIT, Database

MAX_LISTED = 300  # rows of the problem table (the counts above it are always complete)


@dataclass
class FeedRow:
    outlet: str
    outlet_id: str
    url: str
    state: str  # "active" | "failing" | "paused" | "pending"
    status: str | None
    last_success: int | None
    failures: int
    next_attempt: int | None


def feed_status(catalog: Catalog, db: Database, now: int | None = None) -> dict:
    now = now or int(time.time())
    states = {r["url"]: r for r in db.feed_states()}
    rows: list[FeedRow] = []
    counts = {"active": 0, "failing": 0, "paused": 0, "pending": 0}
    for m in catalog.media.values():
        for url in m.feeds:
            st = states.get(url)
            if st is None:
                state = "pending"
            elif st["next_attempt"] and st["next_attempt"] > now:
                state = "paused"
            elif st["failures"] > 0 or not st["last_success"]:
                state = "failing"
            else:
                state = "active"
            counts[state] += 1
            if state != "active":
                rows.append(FeedRow(m.name, m.id, url, state, st["last_status"] if st else None,
                                    st["last_success"] if st else None, st["failures"] if st else 0,
                                    st["next_attempt"] if st else None))
    order = {"paused": 0, "failing": 1, "pending": 2}
    rows.sort(key=lambda r: (order[r.state], -r.failures, r.outlet))
    stats = db.stats()
    attempts = [s["last_attempt"] for s in states.values() if s["last_attempt"]]
    return {
        "feeds_total": sum(counts.values()),
        "counts": counts,
        "problems": rows[:MAX_LISTED],
        "problems_hidden": max(0, len(rows) - MAX_LISTED),
        "outlets": len(catalog.news_media),
        "outlets_with_headlines": stats["media"],
        "articles": stats["articles"],
        "oldest": stats["oldest"],
        "last_ingest": max(attempts) if attempts else None,
        "index_bytes": db.size_bytes(),
        "failure_limit": FEED_FAILURE_LIMIT,
        "now": now,
    }
