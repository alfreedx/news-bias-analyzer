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
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, snapshot, synonyms
from .analysis import (
    Article,
    Story,
    article_from_row,
    blind_spots,
    cluster,
    coverage,
    coverage_summary,
    make_article,
    rank,
    split_sponsored,
    story_score,
)
from .catalog import get_catalog
from .config import BASE_DIR, settings
from .consensus import SIDE_LABEL, Consensus, consensus
from .db import Database
from .factcheck import verdict_cue
from .facts import (
    NO_GROUP_TEXT,
    OWNERSHIP_LABELS,
    PUBLIC_GROUP_KIND_PREFIXES,
    PUBLIC_KIND_QIDS,
    PUBLIC_OWNER_QIDS,
    PUBLIC_TYPE_PREFIXES,
    RELIGIOUS_GROUP_KINDS,
    RELIGIOUS_OWNER_QIDS,
)
from .feeds import ingest_all, search_google_news
from .scoring import DESCRIPTIVE_LABELS, FACTUAL_MAP, IDEOLOGY_MAP, POSITION_LABELS, TERRITORIAL_LABELS, bucket
from .sections import query_is_incident
from .security import RateLimitMiddleware, SecurityHeadersMiddleware
from .sponsored import evidence as sponsored_evidence
from .sponsored import rules_meta as sponsored_rules_meta
from .status import feed_status
from .stories import assign_stories, central_article, link_clusters
from .suggest import MIN_CHARS as SUGGEST_MIN
from .suggest import SuggestCache, normalise, similar_stories, suggest

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
    blind_spots: list = []
    front_updated: int = 0
    ingesting: bool = False


state = State()


def link_stories(stories: list[Story]) -> None:
    """Point each clustered story at the persisted story holding most of its headlines (None when none does).
    No persisted story is claimed by two clustered stories (see `link_clusters`)."""
    story_of = db.story_ids_for([a.id for s in stories for a in s.articles if a.id is not None])
    for s, sid in zip(stories, link_clusters([s.articles for s in stories], story_of), strict=True):
        s.story_id = sid


def build_front_page() -> None:
    since = int(time.time()) - 36 * 3600
    rows = db.recent(since=since, media_ids=[m.id for m in catalog.news_media])
    arts = split_sponsored([article_from_row(catalog, r) for r in rows])[0]
    stories = [s for s in cluster(arts) if s.outlets >= 3]
    stories.sort(key=lambda s: (s.score, max(a.published for a in s.articles)), reverse=True)
    link_stories(stories)
    state.front_page = stories[:8]
    state.blind_spots = blind_spots(stories)
    state.front_updated = int(time.time())


async def ingest_loop() -> None:
    while True:
        state.ingesting = True
        try:
            await ingest_all(catalog, db)
            await asyncio.to_thread(assign_stories, db, catalog)
            await asyncio.to_thread(build_front_page)
            _cache.clear()
            _suggest_cache.clear()
        except Exception:  # keep the loop alive whatever happens
            log.exception("ingestion cycle failed")
        finally:
            state.ingesting = False
        await asyncio.sleep(settings.ingest_interval_minutes * 60)


SNAPSHOT_TIMEOUT = 90.0


async def restore_snapshot() -> bool:
    """Restore the encrypted snapshot into a nearly empty index (Render's disk is wiped on each deploy)."""
    if not (settings.snapshot_url and settings.snapshot_key):
        return False
    try:
        restored = await asyncio.wait_for(
            asyncio.to_thread(snapshot.restore_if_needed, settings.snapshot_url, settings.snapshot_key,
                              settings.db_path, min_articles=settings.snapshot_min_articles,
                              total_timeout=SNAPSHOT_TIMEOUT),
            timeout=SNAPSHOT_TIMEOUT + 15,
        )
        if restored:
            await asyncio.to_thread(Database, settings.db_path)  # migrate an older snapshot
        return restored
    except Exception as exc:  # startup must never fail because of the snapshot
        log.warning("snapshot restore aborted (%s)", type(exc).__name__)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    await restore_snapshot()
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
                   suggest_per_minute=settings.suggest_rate_limit_per_minute,
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


def clock(ts: int) -> str:
    d = datetime.fromtimestamp(ts, TZ)
    return f"{d.day} {MONTHS[d.month - 1][:3]}, {d:%H:%M}"


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


def urlq(value: object) -> str:
    """Percent-encode a value for use inside a query string (nothing is left unescaped)."""
    return quote(str(value), safe="")


def issue_url(title: str, story_id: int) -> str:
    """Prefilled GitHub issue for a badly grouped story: only the story address and its title, no tracking."""
    page = f"{settings.site_url.rstrip('/')}/historia/{story_id}"
    body = "\n".join([f"Historia: {page}", f"Título: {title}", "", "¿Qué titulares no pertenecen a esta historia?"])
    return f"{settings.repo_url}/issues/new?title={urlq('Historia mal agrupada: ' + title[:100])}&body={urlq(body)}"


def sponsored_issue_url() -> str:
    """Prefilled GitHub issue for a missing or wrong «patrocinado» mark: nothing but the questions to answer."""
    body = "\n".join(["Medio:", "Dirección de la pieza:", "",
                       "¿Falta una marca o sobra? Indica cómo la marca el propio medio "
                       "(categoría del RSS, sección de la dirección o prefijo del titular)."])
    return f"{settings.repo_url}/issues/new?title={urlq('Marca de patrocinado: falta o sobra')}&body={urlq(body)}"


templates.env.filters["urlq"] = urlq
templates.env.globals.update(
    issue_url=issue_url, sponsored_issue_url=sponsored_issue_url, clock=clock,
    long_date=long_date, time_ago=time_ago, iso=iso, pos_class=pos_class, score_step=score_step, pct=pct,
    coverage_summary=coverage_summary, verdict_cue=verdict_cue, sponsored_evidence=sponsored_evidence,
    POSITION_LABELS=POSITION_LABELS, bucket=bucket, settings=settings, version=__version__,
)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# --- Search --------------------------------------------------------------------
_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
CACHE_TTL, CACHE_SIZE = 300, 128
TOP_STORIES = 5  # stories shown expanded before the «Más titulares» fold
INCIDENT_FACTOR = 0.25  # relevance multiplier of an item filed under sucesos/tribunales when the query is not
INCIDENT_MIN_OUTLETS = 3  # such a story still reaches the top stories when this many outlets carry it
SIMILAR_BELOW = 5  # «Quizá buscabas…» appears when a search finds fewer headlines than this
_suggest_cache = SuggestCache(size=512, ttl=120)


def clean_query(raw: str) -> str:
    text = "".join(c for c in raw if unicodedata.category(c)[0] != "C")
    return " ".join(text.split())[:MAX_QUERY]


def story_note_lines(notes: dict[int, list[dict]]) -> dict[int, list[dict]]:
    """Persisted story id -> [{word, equivalent_of, outlets}] with outlet names instead of ids."""
    def name(mid: str) -> str:
        m = catalog.media.get(mid)
        return m.name if m else mid
    return {sid: [{**n, "outlets": [name(m) for m in n["media"]]} for n in lines] for sid, lines in notes.items()}


async def run_search(query: str, days: int, broad: bool = False, exact: bool = False) -> dict:
    key = (query.lower(), days, broad, exact)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        _cache.move_to_end(key)
        return cached[1]

    since = int(time.time()) - days * 86400 if days else None
    found = await asyncio.to_thread(
        lambda: db.search_full(query, since=since, broad=broad, exact=exact, fallback=True))
    rows, hidden = found.rows, found.hidden
    bm25 = {r["url"]: r["rank"] for r in rows}
    articles: list[Article] = [article_from_row(catalog, r) for r in rows]

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
    incident_query = query_is_incident(query)
    if not incident_query:  # «incendio en una vivienda» is not what «vivienda» is about: rank it after the topical ones
        for a in articles:
            if a.incident_evidence:
                a.relevance *= INCIDENT_FACTOR
        articles.sort(key=lambda a: a.relevance, reverse=True)
    articles, marked = split_sponsored(articles)  # the outlet's own «patrocinado» marks: listed apart, never counted
    factchecks = [a for a in articles if a.media and a.media.factchecker][:6]
    news = [a for a in articles if not (a.media and a.media.factchecker)]
    stories = cluster(news)
    for s in stories:
        s.incidental = not incident_query and sum(bool(a.incident_evidence) for a in s.articles) * 2 > len(s.articles)
    await asyncio.to_thread(link_stories, stories)
    top, rest = split_top(stories)
    briefs = await asyncio.gather(*(asyncio.to_thread(story_brief, s) for s in top))  # SQLite releases the GIL
    top_info = [{"story": s, **b} for s, b in zip(top, briefs, strict=True)]
    suggestions = []
    if len(found.units) >= 2 and (found.approx or not news):
        suggestions = await asyncio.to_thread(lambda: db.suggest_removals(query, since=since, exact=exact))
    similar: list[dict] = []
    if not exact and len(news) < SIMILAR_BELOW:
        similar = await asyncio.to_thread(find_similar, query, since or 0, news, stories)
    result = {
        "similar": similar,
        "exact": exact,
        "equivalents": found.synonyms_used[:6],
        "story_notes": story_note_lines(found.story_notes),
        "approx": found.approx,
        "approx_words": [u.display for u in found.units],
        "suggestions": suggestions,
        "query": query,
        "days": days,
        "broad": broad,
        "hidden_summary": hidden,
        "articles": news,
        "stories": stories,
        "top": top_info,
        "rest": rest,
        "rest_count": sum(len(s.articles) for s in rest),
        "coverage": coverage(news),
        "factchecks": factchecks,
        "sponsored": marked,
    }
    _cache[key] = (time.time(), result)
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return result


def split_top(stories: list[Story]) -> tuple[list[Story], list[Story]]:
    """(stories shown expanded, the rest in the fold).

    Top: stories carried by 2+ outlets, best score first, at most TOP_STORIES; a story that comes mostly from
    sucesos/tribunales sections (and the query was not about that) needs INCIDENT_MIN_OUTLETS outlets to qualify.
    The rest keeps multi-outlet stories before single headlines and topical before incidental ones.
    """
    eligible = [s for s in stories if len(s.articles) > 1 and (not s.incidental or s.outlets >= INCIDENT_MIN_OUTLETS)]
    top = eligible[:TOP_STORIES]
    ids = {id(s) for s in top}
    rest = sorted((s for s in stories if id(s) not in ids), key=lambda s: (len(s.articles) == 1, s.incidental))
    return top, rest


def find_similar(query: str, since: int | None, news: list[Article], stories: list[Story]) -> list[dict]:
    """Up to 5 persisted stories close to the query that the results do not already include."""
    have = {s.story_id for s in stories if s.story_id}
    have.update(db.story_ids_for([a.id for a in news if a.id is not None]).values())
    return similar_stories(db, query, prefix_last=False, limit=5, since=since, exclude=have)


def parse_days(value: str) -> tuple[str, int]:
    return (value, DAY_OPTIONS[value]) if value in DAY_OPTIONS else ("30", 30)


# --- Pages ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    stats = await asyncio.to_thread(db.stats)
    return render(request, "home.html", stories=state.front_page, blind_spots=state.blind_spots,
                  stats=stats, catalog=catalog, ingesting=state.ingesting)


@app.get("/buscar", response_class=HTMLResponse)
async def search_page(request: Request, q: str = Query("", max_length=500), dias: str = "30",
                      autor: str = Query("", max_length=200), amplio: str = "", exacta: str = ""):
    name = clean_query(autor)
    if name:
        return await author_page(request, name)
    query = clean_query(q)
    days_key, days = parse_days(dias)
    if len(query) < MIN_QUERY:
        return render(request, "search.html", query=query, days_key=days_key, result=None,
                      error="Escribe al menos dos letras." if q else None)
    broad = amplio == "1"
    exact = exacta == "1"
    result = await run_search(query, days, broad, exact)
    groups = sorted({a.media.group.name for a in result["articles"] if a.media and a.media.group})
    return render(request, "search.html", query=query, days_key=days_key, result=result, groups=groups,
                  broad=broad, exact=exact, error=None)


async def author_page(request: Request, name: str):
    """Headlines signed with exactly this name and the outlets where it appears. No profile, no inference."""
    rows = await asyncio.to_thread(db.by_author, name)
    counts = await asyncio.to_thread(db.author_outlets, name)
    articles = [article_from_row(catalog, r) for r in rows]
    outlets = [(catalog.media.get(c["media_id"]), c["n"]) for c in counts]
    return render(request, "author.html", author=name, articles=articles,
                  outlets=[(m, n) for m, n in outlets if m], total=sum(n for _, n in outlets))


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1] if items else ""


def story_summary(c: Consensus, story: Story) -> dict:
    """The «En resumen» lines of a story page, built from the consensus alone (no model, no opinion)."""
    points: list[str] = []
    seen: set[str] = set()
    for p in [*c.terms, *c.pieces]:
        if p.text.lower() not in seen and len(points) < 4:
            seen.add(p.text.lower())
            points.append(p.text)
    sides = [SIDE_LABEL[k] for k in ("left", "center", "right") if story.side_counts[k]]
    differences = []
    for side in ("left", "right"):
        texts = [o.piece.text for o in c.one_sided_terms_of(side)][:3]
        texts = texts or [o.piece.text for o in c.one_sided if o.side == side][:2]
        if texts:
            differences.append((SIDE_LABEL[side], texts))
    return {"points": points if c.enough else [], "voices": c.voices, "enough": c.enough and bool(points),
            "sides_text": _join(sides), "differences": differences}


def fact_checks_for(story: Story, first: int) -> list[Article]:
    """Fact-checker headlines (up to 6) that match the story's shared words, from a day before its first headline."""
    if not story.topic_query:
        return []
    hits = db.search(story.topic_query, since=first - 86400, exact=True)
    return [a for a in (make_article(catalog, title=r["title"], url=r["url"], summary=r["summary"],
                                     published=r["published"], media_id=r["media_id"]) for r in hits)
            if a.media and a.media.factchecker][:6]


def story_brief(story: Story) -> dict:
    """Compact «En resumen» of a story in the search results: shared terms and fact-checks, nothing else."""
    summary = story_summary(consensus(story.articles), story)
    summary["points"] = summary["points"][:3]
    return {"summary": summary, "checks": fact_checks_for(story, min(a.published for a in story.articles))[:2]}


RECENT_SECONDS = 3 * 3600  # a story whose first headline is younger than this is still growing


def load_story(story_id: int) -> dict | None:
    if story_id > 2**62:
        return None
    row = db.get_story(story_id)
    if row is None:
        return None
    articles = [article_from_row(catalog, r) for r in db.story_article_rows(story_id)]
    articles = [a for a in split_sponsored(articles)[0] if not (a.media and a.media.factchecker)]
    if not articles:
        return None
    rank(articles)
    story = Story(articles, seed=central_article(articles), story_id=story_id)
    story.score = story_score(articles)
    story.articles.sort(key=lambda a: (a.position is None, a.position if a.position is not None else 0))
    first = min(a.published for a in articles)
    checks = fact_checks_for(story, first)
    c = consensus(articles)
    return {"story": story, "title": row["title"], "cov": coverage(articles), "consensus": c,
            "summary": story_summary(c, story), "factchecks": checks, "first": first,
            "very_recent": time.time() - first < RECENT_SECONDS,
            "last": max(a.published for a in articles)}


@app.get("/historia/{story_id:int}", response_class=HTMLResponse)
async def story_page(request: Request, story_id: int):
    data = await asyncio.to_thread(load_story, story_id)
    if data is None:
        raise HTTPException(404)
    return render(request, "story.html", **data)


@app.get("/medios", response_class=HTMLResponse)
async def media_index(request: Request):
    media = sorted(catalog.news_media, key=lambda m: (m.assessment.score is None, m.assessment.score or 0, m.name))
    stats = await asyncio.to_thread(db.stats)
    feed_ok = {f["media_id"] for f in stats["feeds"] if f["last_success"]}
    all_labels = sorted({t.label for m in media for t in m.assessment.tags}, key=str.lower)
    regions = sorted({m.region or "Nacional" for m in media})
    languages = sorted({lang for m in media for lang in m.derived.languages})
    territorial = sorted({t for m in media for t in m.assessment.territorial})
    specialities = sorted({m.speciality for m in media if m.speciality})
    return render(request, "media_index.html", media=media, specialities=specialities,
                  factcheckers=catalog.factcheckers,
                  groups=sorted(catalog.groups.values(), key=lambda g: -len(g.media)), feed_ok=feed_ok,
                  all_labels=all_labels, regions=regions, languages=languages, territorial=territorial,
                  ownership=OWNERSHIP_LABELS)


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
    news = catalog.news_media
    counts = {
        "publica": sum(m.derived.ownership == "publica" for m in news),
        "grupo": sum(m.derived.ownership == "grupo" for m in news),
        "sin-grupo": sum(m.derived.ownership == "sin-grupo" for m in news),
        "religiosa": sum(bool(m.derived.religious_text) for m in news),
        "con-ficha": sum(bool(m.facts) for m in news),
        "total": len(news),
    }
    rules = {
        "public_kinds": sorted(PUBLIC_KIND_QIDS.items()), "public_owners": sorted(PUBLIC_OWNER_QIDS.items()),
        "religious_owners": sorted(RELIGIOUS_OWNER_QIDS.items()), "group_kinds": PUBLIC_GROUP_KIND_PREFIXES,
        "types": PUBLIC_TYPE_PREFIXES, "religious_group_kinds": RELIGIOUS_GROUP_KINDS, "no_group": NO_GROUP_TEXT,
    }
    return render(request, "methodology.html", sponsored=sponsored_rules_meta(),
                  synonyms=synonyms.meta(), by_pos=by_pos,
                  territorial=sorted(TERRITORIAL_LABELS.values()),
                  factual=FACTUAL_MAP, audit=audit, descriptive=sorted(DESCRIPTIVE_LABELS), counts=counts,
                  rules=rules)


@app.get("/como-leer", response_class=HTMLResponse)
async def how_to_read(request: Request):
    return render(request, "como_leer.html")


@app.get("/privacidad", response_class=HTMLResponse)
async def privacy(request: Request):
    return render(request, "privacy.html")


@app.get("/estado", response_class=HTMLResponse)
async def service_status(request: Request):
    data = await asyncio.to_thread(feed_status, catalog, db)
    return render(request, "estado.html", s=data)


@app.get("/acerca", response_class=HTMLResponse)
async def about(request: Request):
    return render(request, "about.html")


# --- JSON API ------------------------------------------------------------------
@app.get("/api/buscar")
async def api_search(q: str = Query(..., max_length=500), dias: str = "30", exacta: str = ""):
    query = clean_query(q)
    if len(query) < MIN_QUERY:
        raise HTTPException(422, "La consulta debe tener al menos dos caracteres.")
    _, days = parse_days(dias)
    r = await run_search(query, days, exact=exacta == "1")
    return {
        "query": r["query"],
        "days": r["days"],
        "coverage": r["coverage"].to_public(),
        "stories": [[a.to_public() for a in s.articles] for s in r["stories"]],
        "factchecks": [a.to_public() for a in r["factchecks"]],
        "sponsored": [a.to_public() for a in r["sponsored"]],
    }


@app.get("/api/sugerencias")
async def api_suggestions(q: str = ""):
    """Persisted stories (and, if few, headlines) close to what is being typed. Nothing about `q` is logged."""
    text = normalise(q[:400])
    if len(text) < SUGGEST_MIN:
        return {"suggestions": []}
    key = text.lower()
    items = _suggest_cache.get(key)
    if items is None:
        stories, headlines = await asyncio.to_thread(suggest, db, text)
        items = [{"id": s["id"], "title": s["title"], "outlets": s["outlets"], "url": f"/historia/{s['id']}",
                  "when": time_ago(s["updated"])} for s in stories]
        for h in headlines:
            media = catalog.media.get(h["media_id"])
            items.append({"title": h["title"], "outlet": media.name if media else h["media_id"],
                          "url": f"/buscar?q={urlq(h['keywords'] or h['title'][:60])}"})
        _suggest_cache.put(key, items)
    return {"suggestions": items}


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
    return "User-agent: *\nAllow: /historia/\nDisallow: /buscar\nDisallow: /api/\n"


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
