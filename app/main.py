"""Web application: routes, background ingestion and template helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import unicodedata
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .analysis import Article, cluster, coverage, make_article, rank
from .catalog import get_catalog
from .config import BASE_DIR, settings
from .db import Database
from .feeds import ingest_all, search_google_news
from .scoring import DESCRIPTIVE_LABELS, FACTUAL_MAP, IDEOLOGY_MAP, POSITION_LABELS, TERRITORIAL_LABELS, bucket
from .security import RateLimitMiddleware, SecurityHeadersMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("newsbias")

try:
    TZ = ZoneInfo("Europe/Madrid")
except ZoneInfoNotFoundError:  # Windows without tzdata
    TZ = UTC

catalog = get_catalog()
db = Database(settings.db_path)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

MIN_QUERY, MAX_QUERY = 2, 120
DAY_OPTIONS = {"7": 7, "30": 30, "todo": 0}


# --- Background ingestion ------------------------------------------------------
class State:
    front_page: list = []
    front_updated: int = 0
    ingesting: bool = False


state = State()


def build_front_page() -> None:
    since = int(time.time()) - 36 * 3600
    rows = db.recent(since=since, media_ids=[m.id for m in catalog.news_media])
    arts = [make_article(catalog, title=r["title"], url=r["url"], summary=r["summary"], published=r["published"],
                         media_id=r["media_id"]) for r in rows]
    stories = [s for s in cluster(arts) if s.outlets >= 3]
    stories.sort(key=lambda s: (s.outlets, max(a.published for a in s.articles)), reverse=True)
    state.front_page = stories[:8]
    state.front_updated = int(time.time())


async def ingest_loop() -> None:
    while True:
        state.ingesting = True
        try:
            await ingest_all(catalog, db)
            await asyncio.to_thread(build_front_page)
            _cache.clear()
        except Exception:  # keep the loop alive whatever happens
            log.exception("ingestion cycle failed")
        finally:
            state.ingesting = False
        await asyncio.sleep(settings.ingest_interval_minutes * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    await asyncio.to_thread(build_front_page)
    if settings.ingest_enabled:
        task = asyncio.create_task(ingest_loop())
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="Contraste",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(RateLimitMiddleware, per_minute=settings.rate_limit_per_minute,
                   trust_proxy=settings.trust_proxy_headers)
app.add_middleware(SecurityHeadersMiddleware, https_only=settings.https_only)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts + ["testserver"])
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


# --- Template helpers ----------------------------------------------------------
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def long_date(ts: int | None = None) -> str:
    d = datetime.fromtimestamp(ts or time.time(), TZ)
    return f"{DAYS[d.weekday()].capitalize()}, {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def time_ago(ts: int) -> str:
    diff = int(time.time()) - ts
    if diff < 60:
        return "ahora"
    if diff < 3600:
        return f"hace {diff // 60} min"
    if diff < 86400:
        return f"hace {diff // 3600} h"
    if diff < 7 * 86400:
        n = diff // 86400
        return f"hace {n} día{'s' if n > 1 else ''}"
    d = datetime.fromtimestamp(ts, TZ)
    return f"{d.day} {MONTHS[d.month - 1][:3]} {d.year}"


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def pos_class(position: int | None) -> str:
    return {-2: "l2", -1: "l1", 0: "c0", 1: "r1", 2: "r2"}.get(position, "na")


def score_step(score: float | None) -> int:
    """Map -2..+2 to one of 41 marker positions (CSS classes .at-0 … .at-40, no inline styles under our CSP)."""
    return 20 if score is None else max(0, min(40, round((score + 2) * 10)))


def pct(part: float, whole: float) -> int:
    """Integer percentage for width classes .w-0 … .w-100."""
    return 0 if not whole else max(0, min(100, round(part / whole * 100)))


templates.env.globals.update(
    long_date=long_date, time_ago=time_ago, iso=iso, pos_class=pos_class, score_step=score_step, pct=pct,
    POSITION_LABELS=POSITION_LABELS, bucket=bucket, settings=settings, version=__version__,
)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# --- Search --------------------------------------------------------------------
_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
CACHE_TTL, CACHE_SIZE = 300, 128


def clean_query(raw: str) -> str:
    text = "".join(c for c in raw if unicodedata.category(c)[0] != "C")
    return " ".join(text.split())[:MAX_QUERY]


async def run_search(query: str, days: int) -> dict:
    key = (query.lower(), days)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        _cache.move_to_end(key)
        return cached[1]

    since = int(time.time()) - days * 86400 if days else None
    rows = await asyncio.to_thread(db.search, query, since=since)
    bm25 = {r["url"]: r["rank"] for r in rows}
    articles: list[Article] = [
        make_article(catalog, title=r["title"], url=r["url"], summary=r["summary"], published=r["published"],
                     media_id=r["media_id"])
        for r in rows
    ]

    if settings.google_news_enabled:
        seen = {a.title.lower() for a in articles}
        base = min(bm25.values(), default=-5.0)
        for i, item in enumerate(await search_google_news(query)):
            if since and item.published < since:
                continue
            if item.title.lower() in seen:
                continue
            art = make_article(catalog, title=item.title, url=item.url, summary="", published=item.published,
                               source_url=item.source_url, source_name=item.source_name)
            bm25[art.url] = base * (1 - i / 150)
            articles.append(art)

    rank(articles, bm25)
    factchecks = [a for a in articles if a.media and a.media.factchecker][:6]
    news = [a for a in articles if not (a.media and a.media.factchecker)]
    result = {
        "query": query,
        "days": days,
        "articles": news,
        "stories": cluster(news),
        "coverage": coverage(news),
        "factchecks": factchecks,
    }
    _cache[key] = (time.time(), result)
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return result


def parse_days(value: str) -> tuple[str, int]:
    return (value, DAY_OPTIONS[value]) if value in DAY_OPTIONS else ("30", 30)


# --- Pages ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    stats = await asyncio.to_thread(db.stats)
    return render(request, "home.html", stories=state.front_page, stats=stats, catalog=catalog,
                  ingesting=state.ingesting)


@app.get("/buscar", response_class=HTMLResponse)
async def search_page(request: Request, q: str = Query("", max_length=500), dias: str = "30"):
    query = clean_query(q)
    days_key, days = parse_days(dias)
    if len(query) < MIN_QUERY:
        return render(request, "search.html", query=query, days_key=days_key, result=None,
                      error="Escribe al menos dos letras." if q else None)
    result = await run_search(query, days)
    groups = sorted({a.media.group.name for a in result["articles"] if a.media and a.media.group})
    return render(request, "search.html", query=query, days_key=days_key, result=result, groups=groups,
                  error=None)


@app.get("/medios", response_class=HTMLResponse)
async def media_index(request: Request):
    media = sorted(catalog.news_media, key=lambda m: (m.assessment.score is None, m.assessment.score or 0, m.name))
    stats = await asyncio.to_thread(db.stats)
    feed_ok = {f["media_id"] for f in stats["feeds"] if f["last_success"]}
    all_labels = sorted({t.label for m in media for t in m.assessment.tags}, key=str.lower)
    regions = sorted({m.region or "Nacional" for m in media})
    return render(request, "media_index.html", media=media, factcheckers=catalog.factcheckers,
                  groups=sorted(catalog.groups.values(), key=lambda g: -len(g.media)), feed_ok=feed_ok,
                  all_labels=all_labels, regions=regions)


@app.get("/medios/{media_id}", response_class=HTMLResponse)
async def media_detail(request: Request, media_id: str):
    media = catalog.media.get(media_id)
    if media is None:
        raise HTTPException(404)
    latest = await asyncio.to_thread(db.latest_for_media, media.id, 10)
    return render(request, "media_detail.html", m=media, latest=latest)


@app.get("/grupos/{group_id}", response_class=HTMLResponse)
async def group_detail(request: Request, group_id: str):
    group = catalog.groups.get(group_id)
    if group is None:
        raise HTTPException(404)
    return render(request, "group_detail.html", g=group)


@app.get("/metodologia", response_class=HTMLResponse)
async def methodology(request: Request):
    by_pos: dict[int, list[str]] = {p: [] for p in POSITION_LABELS}
    for label, pos in IDEOLOGY_MAP.items():
        by_pos[pos].append(label)
    audit = {"left": [0, 0], "center": [0, 0], "right": [0, 0]}  # outlets, quoted labels
    for m in catalog.news_media:
        a = m.assessment
        if a.position is None:
            continue
        side = "left" if a.position < 0 else "right" if a.position > 0 else "center"
        audit[side][0] += 1
        audit[side][1] += len(a.tags)
    return render(request, "methodology.html", by_pos=by_pos, territorial=sorted(TERRITORIAL_LABELS.values()),
                  factual=FACTUAL_MAP, audit=audit, descriptive=sorted(DESCRIPTIVE_LABELS))


@app.get("/privacidad", response_class=HTMLResponse)
async def privacy(request: Request):
    return render(request, "privacy.html")


@app.get("/acerca", response_class=HTMLResponse)
async def about(request: Request):
    return render(request, "about.html")


# --- JSON API ------------------------------------------------------------------
@app.get("/api/buscar")
async def api_search(q: str = Query(..., max_length=500), dias: str = "30"):
    query = clean_query(q)
    if len(query) < MIN_QUERY:
        raise HTTPException(422, "La consulta debe tener al menos dos caracteres.")
    _, days = parse_days(dias)
    r = await run_search(query, days)
    return {
        "query": r["query"],
        "days": r["days"],
        "coverage": r["coverage"].to_public(),
        "stories": [[a.to_public() for a in s.articles] for s in r["stories"]],
        "factchecks": [a.to_public() for a in r["factchecks"]],
    }


@app.get("/api/medios")
async def api_media():
    return {"media": [m.to_public() for m in catalog.media.values()],
            "groups": [{"id": g.id, "name": g.name, "owners": g.owners, "sources": g.sources}
                       for g in catalog.groups.values()]}


@app.get("/api/salud")
async def health():
    stats = await asyncio.to_thread(db.stats)
    return {"status": "ok", "version": __version__, "articles": stats["articles"], "last_fetch": stats["last_fetch"]}


# --- Misc ----------------------------------------------------------------------
@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nDisallow: /buscar\nDisallow: /api/\n"


@app.get("/.well-known/security.txt", response_class=PlainTextResponse)
async def security_txt():
    return (f"Contact: {settings.repo_url}/security/advisories/new\n"
            f"Policy: {settings.repo_url}/blob/main/SECURITY.md\n"
            "Preferred-Languages: es, en\n"
            "Expires: 2027-12-31T23:59:59Z\n")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code)
