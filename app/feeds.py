"""Safe RSS/Atom fetching and parsing.

* XML is parsed with defusedxml (no external entities, no entity expansion bombs).
* Responses are size-capped and time-limited.
* Only fixed feed URLs from the versioned catalog are fetched (no user-supplied URLs => no SSRF).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlsplit

import httpx
from defusedxml import ElementTree as SafeET

from .catalog import Catalog
from .config import settings
from .db import Database
from .text import clean_text, fold

log = logging.getLogger("newsbias.feeds")
# httpx logs full request URLs at INFO level; a Google News URL would contain the
# user's search terms, so keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_FEED_BYTES = 5 * 1024 * 1024
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
DC_SUBJECT = "{http://purl.org/dc/elements/1.1/}subject"
RSS1 = "{http://purl.org/rss/1.0/}"


@dataclass
class FeedItem:
    title: str
    url: str
    summary: str
    published: int
    source_url: str | None = None  # only for aggregators (Google News <source url="">)
    source_name: str | None = None
    author: str | None = None  # byline exactly as published (cleaned), never enriched
    categories: tuple[str, ...] = ()  # <category>/<dc:subject> labels exactly as the outlet publishes them


# Bylines that say nothing about a person. Agencies named as such (EFE, Europa Press) are kept on purpose.
GENERIC_AUTHORS = frozenset({
    "redaccion", "la redaccion", "redaccion web", "equipo de redaccion", "agencias", "agencia", "staff", "admin",
    "administrador", "editor", "equipo", "webmaster", "autor", "anonimo", "desconocido", "unknown", "author",
})
MAX_AUTHOR = 120
_EMAIL_WRAP = re.compile(r"^\S+@\S+\s*\((.+)\)\s*$")
_BY_PREFIX = re.compile(r"^(por|by|texto de|texto:)\s+", re.I)


def clean_author(raw: str | None, outlet_name: str | None = None) -> str | None:
    """Normalise a feed byline: no e-mails, no «Por », no URLs, collapsed spaces, <= 120 chars.

    Returns None for empty, generic ("Redacción", "Agencias") or outlet-name bylines.
    """
    if not raw:
        return None
    text = clean_text(raw, 400)
    wrapped = _EMAIL_WRAP.match(text)  # RSS 2.0 style: "jane@x.com (Jane Doe)"
    if wrapped:
        text = wrapped.group(1)
    text = " ".join(part for part in text.split() if "@" not in part and "://" not in part)
    text = _BY_PREFIX.sub("", text).strip(" ,;:-–—·|/()[]")
    text = " ".join(re.sub(r"\s*,\s*", ", ", text).split())
    if len(text) > MAX_AUTHOR:
        text = text[:MAX_AUTHOR].rsplit(" ", 1)[0].strip(" ,;")
    if not text or len(text) < 2:
        return None
    key = fold(text)
    if key in GENERIC_AUTHORS or (outlet_name and key == fold(outlet_name)):
        return None
    # «Redacción El Debate», «Redacción Hoy»: the newsroom, not a person
    if key.startswith(("redaccion ", "la redaccion ", "agencias ")):
        return None
    return text


MAX_CATEGORIES, MAX_CATEGORY_LEN = 12, 60
CATEGORY_SEP = " | "


def _categories(el, *, atom: bool) -> tuple[str, ...]:
    """The outlet's own labels of an item: RSS <category>, <dc:subject>, Atom <category term/label>."""
    raw: list[str | None] = []
    if atom:
        for node in el.findall(f"{ATOM}category"):
            raw.append(node.get("label") or node.get("term") or node.text)
    else:
        raw += [n.text for n in el.findall("category")]
        raw += [n.text for n in el.findall(DC_SUBJECT)]
    out: dict[str, None] = {}
    for value in raw:
        text = clean_text(value, 200) if value else ""
        if text:
            out.setdefault(text[:MAX_CATEGORY_LEN].strip(), None)
    return tuple(list(out)[:MAX_CATEGORIES])


def join_categories(categories) -> str | None:
    """Storage form of the categories (short, joined, capped); None when there are none."""
    return CATEGORY_SEP.join(categories)[:400] or None


def _authors(el, *tags: str) -> str | None:
    names = []
    for tag in tags:
        for node in el.findall(tag):
            inner = node.find(f"{ATOM}name")
            names.append(inner.text if inner is not None and inner.text else node.text)
    names = [n for n in names if n and n.strip()]
    return ", ".join(dict.fromkeys(n.strip() for n in names)) or None


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or len(value) > 2048:
        return None
    return value


def _parse_date(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _text(el, tag: str) -> str | None:
    node = el.find(tag)
    return node.text if node is not None and node.text else None


def parse_feed(content: bytes, now: int | None = None) -> list[FeedItem]:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom. Returns [] on malformed input."""
    now = now or int(time.time())
    try:
        root = SafeET.fromstring(content)
    except Exception:  # defusedxml raises several exception types; all mean "unusable feed"
        return []

    items: list[FeedItem] = []
    entries = root.findall(f"{ATOM}entry")
    if entries:
        for e in entries:
            link = None
            for ln in e.findall(f"{ATOM}link"):
                if ln.get("rel", "alternate") == "alternate":
                    link = ln.get("href")
                    break
            title = clean_text(_text(e, f"{ATOM}title"), 300)
            summary = clean_text(_text(e, f"{ATOM}summary") or _text(e, f"{ATOM}content"))
            pub = _parse_date(_text(e, f"{ATOM}published") or _text(e, f"{ATOM}updated"))
            url = _safe_url(link)
            if title and url:
                items.append(FeedItem(title, url, summary, min(pub or now, now),
                                      author=clean_author(_authors(e, f"{ATOM}author")),
                                      categories=_categories(e, atom=True)))
        return items

    nodes = root.findall("./channel/item") or root.findall(f"{RSS1}item")
    for it in nodes:
        title = clean_text(_text(it, "title") or _text(it, f"{RSS1}title"), 300)
        url = _safe_url(_text(it, "link") or _text(it, f"{RSS1}link"))
        summary = clean_text(_text(it, "description") or _text(it, CONTENT) or _text(it, f"{RSS1}description"))
        pub = _parse_date(_text(it, "pubDate") or _text(it, DC_DATE))
        source = it.find("source")
        item = FeedItem(title, url or "", summary, min(pub or now, now),
                        author=clean_author(_authors(it, DC_CREATOR, "author")),
                        categories=_categories(it, atom=False))
        if source is not None:
            item.source_url = _safe_url(source.get("url"))
            item.source_name = clean_text(source.text, 120) or None
        if title and url:
            items.append(item)
    return items


async def _download(client: httpx.AsyncClient, url: str, headers: dict) -> tuple[int, bytes, httpx.Headers]:
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code != 200:
            return resp.status_code, b"", resp.headers
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise ValueError("feed too large")
            chunks.append(chunk)
        return 200, b"".join(chunks), resp.headers


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        },
    )


async def _ingest_feed(client: httpx.AsyncClient, db: Database, catalog: Catalog, media_id: str, url: str) -> int:
    state = db.get_feed_state(url)
    headers = {}
    if state is not None:
        if state["etag"]:
            headers["If-None-Match"] = state["etag"]
        if state["last_modified"]:
            headers["If-Modified-Since"] = state["last_modified"]
    try:
        status, body, resp_headers = await _download(client, url, headers)
    except (httpx.HTTPError, ValueError) as exc:
        db.save_feed_state(url, media_id, etag=None, last_modified=None, status=type(exc).__name__, items=0,
                           success=False)
        log.warning("feed %s failed: %s", media_id, type(exc).__name__)
        return 0
    if status == 304:
        db.save_feed_state(url, media_id, etag=None, last_modified=None, status="304", items=0, success=True)
        return 0
    if status != 200:
        db.save_feed_state(url, media_id, etag=None, last_modified=None, status=str(status), items=0, success=False)
        log.warning("feed %s returned HTTP %s", media_id, status)
        return 0
    items = parse_feed(body)
    outlet = catalog.media[media_id].name if media_id in catalog.media else None
    added = db.insert_articles((i.url, media_id, i.title, i.summary, i.published,
                                clean_author(i.author, outlet), join_categories(i.categories)) for i in items)
    db.save_feed_state(url, media_id, etag=resp_headers.get("etag"), last_modified=resp_headers.get("last-modified"),
                       status="200" if items else "200-empty", items=len(items), success=bool(items))
    return added


PER_HOST_LIMIT = 2  # simultaneous requests to one host (several feeds of an outlet share its server)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


async def ingest_all(catalog: Catalog, db: Database, concurrency: int | None = None,
                     per_host: int = PER_HOST_LIMIT, client: httpx.AsyncClient | None = None) -> int:
    """Fetch every catalog feed once. Returns number of new articles.

    At most `concurrency` requests run at once (default: INGEST_CONCURRENCY, 8) and at most `per_host` to the same
    host. Feeds that failed too many times in a row are paused for a while (`Database.feed_due`).
    """
    sem = asyncio.Semaphore(concurrency or settings.ingest_concurrency)
    hosts: dict[str, asyncio.Semaphore] = {}
    now = int(time.time())
    due = [(m.id, f) for m in catalog.media.values() for f in m.feeds if db.feed_due(db.get_feed_state(f), now)]
    skipped = sum(len(m.feeds) for m in catalog.media.values()) - len(due)

    async def run(http: httpx.AsyncClient, media_id: str, url: str) -> int:
        host_sem = hosts.setdefault(host_of(url), asyncio.Semaphore(per_host))
        async with host_sem, sem:
            return await _ingest_feed(http, db, catalog, media_id, url)

    if client is not None:
        results = await asyncio.gather(*(run(client, m, f) for m, f in due), return_exceptions=True)
    else:
        async with make_client() as http:
            results = await asyncio.gather(*(run(http, m, f) for m, f in due), return_exceptions=True)
    added = sum(r for r in results if isinstance(r, int))
    purged = db.purge_older_than(settings.retention_days)
    log.info("ingest finished: %d new articles, %d purged, %d feeds paused", added, purged, skipped)
    return added


async def search_google_news(query: str) -> list[FeedItem]:
    """Optional live search through Google News RSS (personal/non-commercial use only)."""
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=es&gl=ES&ceid=ES:es"
    try:
        async with make_client() as client:
            status, body, _ = await _download(client, url, {})
    except (httpx.HTTPError, ValueError):
        return []
    if status != 200:
        return []
    items = parse_feed(body)
    for item in items:
        # Google appends " - Outlet" to every title.
        if item.source_name and item.title.endswith(f" - {item.source_name}"):
            item.title = item.title[: -len(item.source_name) - 3].strip()
    return items
