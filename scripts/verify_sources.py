"""Check that every source cited in data/*.json is reachable and still says what we claim.

* Wikipedia and Wikidata pages are checked in batches through their official APIs.
* Media Bias/Fact Check pages must still publish the same "Bias Rating" and
  "Factual Reporting" (read from the page's JSON-LD) that we store.
* Every RSS feed must return a parsable feed with items (use --no-feeds to skip).

Usage:  python scripts/verify_sources.py [--no-feeds]
Exit code 1 if anything needs attention.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.feeds import parse_feed  # noqa: E402

# Wikimedia asks automated clients for a descriptive User-Agent. Set VERIFY_CONTACT
# (a URL or e-mail) to include contact details, as their policy recommends.
UA = "NewsBiasAnalyzer-verify/1.0 (open-source source checker" + (
    f"; {os.environ['VERIFY_CONTACT']})" if os.environ.get("VERIFY_CONTACT") else ")"
)
BIAS_RE = re.compile(r'"name":"Bias Rating","value":"([^"]+)"')
FACT_RE = re.compile(r'"name":"Factual Reporting","value":"([^"]+)"')


def request(client: httpx.Client, url: str, **kwargs) -> httpx.Response | None:
    """GET with polite back-off on throttling (403/429/5xx)."""
    r = None
    for attempt in range(4):
        try:
            r = client.get(url, **kwargs)
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code not in (403, 429, 500, 502, 503):
            return r
        time.sleep(5 * 2**attempt)
    return r


UNVERIFIED: set[str] = set()  # URLs we could not check (throttled / network)


def check_wikipedia(client: httpx.Client, urls: set[str]) -> set[str]:
    """Return the URLs whose article does not exist."""
    by_host: dict[str, dict[str, str]] = defaultdict(dict)
    for url in urls:
        p = urlsplit(url)
        by_host[p.netloc][unquote(p.path[len("/wiki/"):]).replace("_", " ")] = url
    missing: set[str] = set()
    for host, titles in by_host.items():
        names = list(titles)
        for i in range(0, len(names), 50):
            chunk = names[i:i + 50]
            r = request(client, f"https://{host}/w/api.php", params={
                "action": "query", "format": "json", "redirects": 1, "titles": "|".join(chunk)})
            if r is None or r.status_code != 200:
                UNVERIFIED.update(titles[t] for t in chunk)
                continue
            data = r.json()["query"]
            normalized = {n["from"]: n["to"] for n in data.get("normalized", [])}
            redirects = {n["from"]: n["to"] for n in data.get("redirects", [])}
            found = {pg["title"] for pg in data.get("pages", {}).values() if "missing" not in pg}
            for t in chunk:
                name = normalized.get(t, t)
                name = redirects.get(name, name)
                if name not in found:
                    missing.add(titles[t])
            time.sleep(1)
    return missing


def check_wikidata(client: httpx.Client, urls: set[str]) -> set[str]:
    ids = {urlsplit(u).path.rsplit("/", 1)[-1]: u for u in urls}
    missing: set[str] = set()
    keys = list(ids)
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        r = request(client, "https://www.wikidata.org/w/api.php", params={
            "action": "wbgetentities", "format": "json", "props": "info", "ids": "|".join(chunk)})
        if r is None or r.status_code != 200:
            UNVERIFIED.update(ids[q] for q in chunk)
            continue
        entities = r.json().get("entities", {})
        for q in chunk:
            if q not in entities or "missing" in entities[q]:
                missing.add(ids[q])
    return missing


def main() -> int:
    check_feeds = "--no-feeds" not in sys.argv
    media = json.loads((ROOT / "data" / "media.json").read_text(encoding="utf-8"))["media"]
    groups = json.loads((ROOT / "data" / "groups.json").read_text(encoding="utf-8"))["groups"]
    problems: list[str] = []

    # who cites each URL, for readable reports
    citations: dict[str, list[str]] = defaultdict(list)
    for m in media:
        for ev in m["evidence"]:
            citations[ev["url"]].append(m["id"])
    for g in groups:
        for s in g["sources"]:
            citations[s["url"]].append(f"group {g['id']}")

    wiki = {u for u in citations if urlsplit(u).netloc.endswith("wikipedia.org")}
    wikidata = {u for u in citations if urlsplit(u).netloc == "www.wikidata.org"}
    others = set(citations) - wiki - wikidata

    with httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True) as client:
        for url in sorted(check_wikipedia(client, wiki) | check_wikidata(client, wikidata)):
            problems.append(f"{', '.join(citations[url])}: page not found {url}")
        print(f"Wikipedia: {len(wiki)} pages · Wikidata: {len(wikidata)} items checked", flush=True)

        pages: dict[str, str | None] = {}
        for url in sorted(others):
            r = request(client, url)
            pages[url] = r.text if r is not None and r.status_code == 200 else None
            if pages[url] is None:
                problems.append(f"{', '.join(citations[url])}: {url} -> {getattr(r, 'status_code', 'error')}")
            time.sleep(1)
        print(f"Other pages: {len(others)} checked", flush=True)

        for m in media:
            for ev in m["evidence"]:
                if ev["source"] != "mbfc" or not pages.get(ev["url"]):
                    continue
                regex = BIAS_RE if ev["kind"] == "ideology" else FACT_RE
                found = regex.search(pages[ev["url"]])
                claimed = ev["labels"][0]
                if not found:
                    problems.append(f"{m['id']}: MBFC page shows no {ev['kind']} rating")
                elif found.group(1).lower() != claimed.lower():
                    problems.append(f"{m['id']}: MBFC {ev['kind']} changed: we say {claimed!r}, "
                                    f"MBFC now says {found.group(1)!r}")

            if check_feeds:
                for feed in m["feeds"]:
                    try:  # fetched exactly like the app does
                        r = client.get(feed, headers={"User-Agent": settings.user_agent})
                    except httpx.HTTPError:
                        r = None
                    items = parse_feed(r.content) if r is not None and r.status_code == 200 else []
                    if not items:
                        problems.append(f"{m['id']}: feed unusable {feed} -> {getattr(r, 'status_code', 'error')}")
        if check_feeds:
            print(f"Feeds: {sum(len(m['feeds']) for m in media)} checked", flush=True)

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        return 1
    print("\nEverything matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
