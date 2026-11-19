"""Measure which outlets mark sponsored content in their own feeds, before writing any rule.

Fetches every feed in data/media.json once (polite: at most 6 at a time, the app's User-Agent, 15 s timeout)
and counts, per outlet, the items carrying a CANDIDATE signal of sponsorship that the outlet itself publishes:
RSS <category>/<dc:subject>/Atom category values, URL path segments and title prefixes. It also lists the
category values seen across all feeds (top 200) so labels nobody anticipated can be spotted by eye.

Nothing here decides anything: the rules that the site applies live in data/sponsored_rules.json and are
written by hand after reading this report (examples included, to catch false positives).

Usage:  python scripts/sponsored_survey.py      (writes var/sponsored_survey.json)
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.feeds import _download, make_client, parse_feed  # noqa: E402
from app.text import fold  # noqa: E402

OUT = ROOT / "var" / "sponsored_survey.json"

# Broad on purpose: this is the net; the final rules are the narrower, reviewed subset.
CANDIDATES = [
    "patrocinado", "contenido patrocinado", "publirreportaje", "branded", "brand content", "brandcontent",
    "contenido de marca", "publicidad", "promocionado", "sponsored", "en colaboracion con", "ofrecido por",
    "partner content",
]
TITLE_PREFIX = re.compile(
    r"^\s*[\[(]?\s*(patrocinado|contenido patrocinado|publirreportaje|publicidad|branded content|"
    r"contenido de marca|promocionado|sponsored|publi)\s*[\])]?\s*[:\-|]", re.I)
TITLE_BRACKET = re.compile(r"^\s*[\[(]\s*(publicidad|patrocinado|publi|sponsored|publirreportaje)\s*[\])]", re.I)


def signals(item) -> list[tuple[str, str, str]]:
    """(field, candidate pattern, evidence text) for every candidate signal of one item."""
    found: list[tuple[str, str, str]] = []
    for cat in item.categories:
        folded = fold(cat)
        found += [("category", p, cat) for p in CANDIDATES if p in folded]
    segments = [s for s in urlsplit(item.url).path.split("/") if s]
    for seg in segments[:-1]:  # the last segment is the slug of the piece itself
        folded = fold(seg).replace("-", " ").replace("_", " ")
        found += [("path", p, seg) for p in CANDIDATES if p in folded]
    folded_title = fold(item.title)
    for rx in (TITLE_PREFIX, TITLE_BRACKET):
        m = rx.match(folded_title)
        if m:
            found.append(("title_prefix", m.group(1), item.title[:60]))
            break
    return found


async def main() -> int:
    media = json.loads((ROOT / "data" / "media.json").read_text(encoding="utf-8"))["media"]
    jobs = [(m["id"], f) for m in media for f in m.get("feeds", [])]
    sem = asyncio.Semaphore(6)
    per_outlet: dict[str, dict] = defaultdict(lambda: {"items": 0, "with_categories": 0, "matches": Counter(),
                                                       "examples": {}, "feeds_ok": 0, "feeds": 0})
    categories: Counter[str] = Counter()
    failures: list[str] = []

    async with make_client() as client:
        async def run(mid: str, url: str) -> None:
            async with sem:
                st = per_outlet[mid]
                st["feeds"] += 1
                try:
                    status, body, _ = await _download(client, url, {})
                except Exception as exc:  # network errors are reported, never fatal
                    failures.append(f"{mid}: {type(exc).__name__}")
                    return
            items = parse_feed(body) if status == 200 else []
            if not items:
                failures.append(f"{mid}: HTTP {status}, no items")
                return
            st["feeds_ok"] += 1
            for it in items:
                st["items"] += 1
                if it.categories:
                    st["with_categories"] += 1
                    categories.update(it.categories)
                for fld, pat, ev in signals(it):
                    key = f"{fld}:{pat}"
                    st["matches"][key] += 1
                    st["examples"].setdefault(key, []).append({"title": it.title[:90], "url": it.url, "evidence": ev})

        await asyncio.gather(*(run(mid, url) for mid, url in jobs))

    names = {m["id"]: m["name"] for m in media}
    total_items = sum(s["items"] for s in per_outlet.values())
    by_pattern: Counter[str] = Counter()
    for s in per_outlet.values():
        by_pattern.update(s["matches"])
    flagged = {mid: s for mid, s in per_outlet.items() if s["matches"]}

    print(f"feeds fetched: {sum(s['feeds_ok'] for s in per_outlet.values())}/{len(jobs)}; items scanned: {total_items}")
    with_cats = sum(1 for s in per_outlet.values() if s["with_categories"])
    print(f"outlets exposing categories: {with_cats}/{len(per_outlet)}")
    print(f"items with categories: {sum(s['with_categories'] for s in per_outlet.values())}")
    print("\nmatches by candidate pattern (field:pattern -> items):")
    for key, n in by_pattern.most_common():
        print(f"  {n:5d}  {key}")
    print("\noutlets with any candidate signal:")
    for mid, s in sorted(flagged.items(), key=lambda kv: -sum(kv[1]["matches"].values())):
        print(f"  {names[mid]:28s} items={s['items']:4d} " + ", ".join(f"{k}={v}" for k, v in s["matches"].items()))
        for key, exs in s["examples"].items():
            for ex in exs[:2]:
                print(f"       [{key}] «{ex['title']}» ({ex['evidence']}) {ex['url']}")
    print("\nfailed feeds:", failures or "none")

    result = {
        "feeds_fetched": sum(s["feeds_ok"] for s in per_outlet.values()), "feeds_total": len(jobs),
        "items_scanned": total_items,
        "by_pattern": dict(by_pattern),
        "outlets": {mid: {"name": names[mid], "items": s["items"], "with_categories": s["with_categories"],
                          "matches": dict(s["matches"]), "examples": {k: v[:5] for k, v in s["examples"].items()}}
                    for mid, s in per_outlet.items()},
        "top_categories": categories.most_common(200),
        "failures": failures,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nfull output: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
