"""Hit rate of the search on the gold set (tests/search_cases.json): literal words only vs equivalent words.

    python scripts/eval_search.py            # seeds every case into a temporary index (what the unit test does)
    python scripts/eval_search.py --db var/news.sqlite3   # runs the same queries on a real index

A case is a hit when one of its `expect` URL fragments appears among the first 10 results.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402

CASES_FILE = ROOT / "tests" / "search_cases.json"
TOP = 10


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def seed_case(db: Database, case: dict) -> None:
    """Insert the case's headlines (published now) and, when it has one, the persisted story linking them."""
    now = int(time.time())
    db.insert_articles([(a["url"], a["media_id"], a["title"], a["summary"], now - i * 60)
                        for i, a in enumerate(case["seed"])])
    if case.get("story"):
        with db.connect() as conn:
            marks = ",".join("?" * len(case["story"]))
            ids = [r[0] for r in conn.execute(f"SELECT id FROM articles WHERE url IN ({marks})", case["story"])]  # noqa: S608
            sid = conn.execute("INSERT INTO stories(created, updated, title, keywords) VALUES (?, ?, 'gold', '')",
                               (now, now)).lastrowid
            conn.executemany("INSERT INTO story_articles(story_id, article_id) VALUES (?, ?)", [(sid, i) for i in ids])


def hit(rows: list, expect: list[str]) -> bool:
    return any(e in r["url"] for r in rows[:TOP] for e in expect)


def run_case(db: Database, case: dict) -> tuple[bool, bool]:
    """(literal words only, equivalent words + story level) hit. Ordering is the search's own (best first)."""
    strict = db.search_full(case["query"], exact=True).rows
    wide = db.search_full(case["query"]).rows
    return hit(strict, case["expect"]), hit(wide, case["expect"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="evaluate on this index instead of seeding the cases")
    args = ap.parse_args()
    results = []
    for case in load_cases():
        if args.db:
            results.append((case["query"], *run_case(Database(args.db), case)))
        else:
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "gold.sqlite3")
                seed_case(db, case)
                results.append((case["query"], *run_case(db, case)))
    for query, strict, wide in results:
        flag = lambda ok: "hit " if ok else "miss"  # noqa: E731
        print(f"{'ok ' if wide else 'MISS'}  literal={flag(strict)}  expanded={flag(wide)}  {query}")
    n = len(results)
    print(f"\nTop-{TOP} hit rate over {n} queries: literal {sum(r[1] for r in results)}/{n}, "
          f"expanded {sum(r[2] for r in results)}/{n}")


if __name__ == "__main__":
    main()
