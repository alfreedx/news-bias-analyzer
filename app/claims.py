"""Fact-checks from the checkers' own structured data (schema.org ClaimReview JSON-LD).

Contraste never rates anything. A fact-checker page may embed a machine-readable ClaimReview: the claim, the
rating as the checker words it, who said it and where it appeared. We store those fields verbatim and link a
review to a story only when the checker itself names the URL of one of the story's articles.

* JSON is read with `json.loads` only (never evaluated); pages are size-capped; malformed data is ignored.
* Only the checker's own domain is fetched, over http(s), with our User-Agent, honouring robots.txt, at most two
  requests at once per host. An HTTP 401/403/429 means "we are not welcome": no retry, the host rests for 24 h.
* A failure here never affects the main ingest (the caller wraps it, and every error is logged and skipped).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .catalog import Catalog
from .config import settings
from .db import Database
from .text import clean_text, normalize_url, safe_url  # noqa: F401 (normalize_url re-exported)

log = logging.getLogger("newsbias.claims")

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_CLAIM = 300
MAX_RATING = 120
MAX_CLAIMANT = 120
MAX_REVIEWED_URLS = 10
MAX_JSON_DEPTH = 8
HOST_PAUSE = 24 * 3600  # a host that answered 403/429 is left alone this long
ERROR_RETRY = 6 * 3600  # transient failures (timeouts, 5xx) are retried after this long
PER_HOST = 2
RECENT_DAYS = 120  # only pages of checker articles still in the index are fetched
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
REFUSED = (401, 403, 429)

_LD_JSON = re.compile(r"<script\b[^>]*\btype\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script\s*>",
                      re.I | re.S)


# --- URLs -------------------------------------------------------------------------
def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def on_own_domain(url: str, domains: list[str]) -> bool:
    """True when the URL's host is one of the outlet's domains or a subdomain of one."""
    host = _host(url)
    own = [d.lower().removeprefix("www.") for d in domains]
    return bool(host) and any(host == d or host.endswith("." + d) for d in own)


# --- Parsing ----------------------------------------------------------------------
@dataclass
class ClaimReview:
    claim: str
    rating: str | None = None
    claimant: str | None = None
    reviewed_urls: list[str] = field(default_factory=list)
    published: int | None = None


def _s(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return clean_text(value, limit) or None


def _types(node: dict) -> list[str]:
    t = node.get("@type")
    items = t if isinstance(t, list) else [t]
    return [x.rsplit("/", 1)[-1] for x in items if isinstance(x, str)]


def _name(value) -> str | None:
    """Name of a Person/Organization node, a bare string, or the first name of a list of them."""
    if isinstance(value, list):
        return next((n for n in (_name(v) for v in value) if n), None)
    if isinstance(value, dict):
        return _s(value.get("name"), MAX_CLAIMANT)
    return _s(value, MAX_CLAIMANT)


def _urls(value) -> list[str]:
    if isinstance(value, list):
        return [u for v in value for u in _urls(v)]
    if isinstance(value, dict):
        return _urls(value.get("url") or value.get("@id"))
    u = safe_url(value)
    return [u] if u else []


def _date(value) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        return min(int(dt.timestamp()), int(time.time()))
    except (OverflowError, OSError, ValueError):
        return None


def _review_from(node: dict) -> ClaimReview | None:
    claim = _s(node.get("claimReviewed"), MAX_CLAIM)
    if not claim:
        return None
    rating = node.get("reviewRating")
    text = _s(rating.get("alternateName"), MAX_RATING) if isinstance(rating, dict) else None
    item = node.get("itemReviewed")
    items = [i for i in (item if isinstance(item, list) else [item]) if isinstance(i, dict)]
    claimant = next((n for n in (_name(i.get("author")) for i in items) if n), None)
    urls: list[str] = []
    for i in items:
        for key in ("appearance", "firstAppearance", "url"):
            urls += _urls(i.get(key))
    urls = list(dict.fromkeys(urls))[:MAX_REVIEWED_URLS]
    return ClaimReview(claim, text, claimant, urls, _date(node.get("datePublished")))


def _walk(node, out: list[dict], depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        return
    if isinstance(node, list):
        for n in node:
            _walk(n, out, depth + 1)
    elif isinstance(node, dict):
        if "ClaimReview" in _types(node):
            out.append(node)
        for key in ("@graph", "mainEntity", "hasPart"):
            if key in node:
                _walk(node[key], out, depth + 1)


def parse_claim_reviews(html: str | bytes) -> list[ClaimReview]:
    """Every ClaimReview in the page's JSON-LD blocks (lists, @graph and nesting included); [] when none."""
    if isinstance(html, bytes):
        html = html[:MAX_PAGE_BYTES].decode("utf-8", errors="replace")
    nodes: list[dict] = []
    for block in _LD_JSON.findall(html[:MAX_PAGE_BYTES]):
        try:
            data = json.loads(block.strip())
        except (ValueError, RecursionError):
            continue
        _walk(data, nodes)
    seen: set[str] = set()
    unique = []
    for review in (_review_from(n) for n in nodes):
        if review and review.claim not in seen:
            seen.add(review.claim)
            unique.append(review)
    return unique


# --- Fetching ---------------------------------------------------------------------
class Robots:
    """robots.txt of each host, fetched once per run. `check` answers "ok", "disallowed", "refused" (the host
    answered 401/403/429: it does not want us) or "unavailable" (network error or 5xx: try again next cycle).
    A plain 404/410 means, as usual, no restrictions."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cache: dict[str, RobotFileParser | str] = {}

    async def check(self, url: str) -> str:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.cache:
            self.cache[origin] = await self._load(origin)
        entry = self.cache[origin]
        if isinstance(entry, str):
            return entry
        return "ok" if entry.can_fetch(settings.user_agent, url) else "disallowed"

    async def _load(self, origin: str) -> RobotFileParser | str:
        parser = RobotFileParser()
        try:
            resp = await self.client.get(origin + "/robots.txt", headers={"Accept": "text/plain"})
        except httpx.HTTPError:
            return "unavailable"
        if resp.status_code in (404, 410):
            parser.parse([])
            return parser
        if resp.status_code in REFUSED:
            return "refused"
        if resp.status_code != 200:
            return "unavailable"
        parser.parse(resp.text[:512 * 1024].splitlines())
        return parser


async def _download_page(client: httpx.AsyncClient, url: str) -> tuple[int, bytes]:
    async with client.stream("GET", url, headers={"Accept": "text/html,application/xhtml+xml;q=0.9"}) as resp:
        if resp.status_code != 200:
            return resp.status_code, b""
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_PAGE_BYTES:
                raise ValueError("page too large")
            chunks.append(chunk)
        return 200, b"".join(chunks)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": settings.user_agent})


async def fetch_claims(catalog: Catalog, db: Database, *, limit: int | None = None,
                       client: httpx.AsyncClient | None = None) -> int:
    """Fetch up to `limit` (CLAIMS_PER_CYCLE) not-yet-read pages of fact-checker articles and store their
    ClaimReviews. Returns how many reviews were stored. Never raises for network or data problems."""
    limit = settings.claims_per_cycle if limit is None else limit
    checkers = {m.id: m for m in catalog.factcheckers}
    now = int(time.time())
    purged = db.purge_claims(settings.claims_retention_days)
    if limit <= 0 or not checkers:
        return 0
    resting = db.hosts_resting(now)
    # a checker whose host is resting must not crowd the others out of the (limited) candidate list
    active = [mid for mid, m in checkers.items() if not any(_host(f"https://{d}") in resting for d in m.domains)]
    pending = db.claim_pages_pending(active, since=now - RECENT_DAYS * 86400,
                                     retry_before=now - ERROR_RETRY, limit=limit * 3)
    todo = []
    for row in pending:
        media = checkers[row["media_id"]]
        if not safe_url(row["url"]) or not on_own_domain(row["url"], media.domains):
            db.save_claim_page(row["url"], row["media_id"], "skipped")
        elif _host(row["url"]) not in resting:
            todo.append(row)
        if len(todo) >= limit:
            break
    if not todo:
        return 0

    sems: dict[str, asyncio.Semaphore] = {}
    refused: set[str] = set()
    owns_client = client is None
    http = client or make_client()
    robots = Robots(http)

    async def one(row) -> int:
        url, host = row["url"], _host(row["url"])
        async with sems.setdefault(host, asyncio.Semaphore(PER_HOST)):
            if host in refused:  # another request of this run was refused: no retry
                return 0
            try:
                verdict = await robots.check(url)
                if verdict == "disallowed":
                    db.save_claim_page(url, row["media_id"], "robots")
                    return 0
                if verdict == "unavailable":
                    return 0
                status, body = (403, b"") if verdict == "refused" else await _download_page(http, url)
            except (httpx.HTTPError, ValueError) as exc:
                db.save_claim_page(url, row["media_id"], "error")
                log.warning("claim page of %s failed: %s", row["media_id"], type(exc).__name__)
                return 0
            if status in REFUSED:
                refused.add(host)
                db.rest_host(host, status, HOST_PAUSE)
                log.warning("%s answered HTTP %s: resting 24 h", host, status)
                return 0
            if status != 200:
                db.save_claim_page(url, row["media_id"], "error" if status >= 500 else str(status))
                return 0
            reviews = parse_claim_reviews(body)
            return db.save_claim_reviews(url, row["media_id"], row["title"], row["published"], reviews)

    stored = 0
    try:
        results = await asyncio.gather(*(one(r) for r in todo), return_exceptions=True)
    finally:
        if owns_client:
            await http.aclose()
    for r in results:
        if isinstance(r, int):
            stored += r
        else:
            log.warning("claim page task failed: %s", type(r).__name__)
    log.info("claims: %d pages read, %d reviews stored, %d purged", len(todo), stored, purged)
    return stored
