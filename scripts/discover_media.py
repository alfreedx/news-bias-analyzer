"""Discover Spanish news outlets that are not in data/media.json yet (Fase 1 of docs/PLAN-COBERTURA.md).

Pipeline (re-runnable, polite, no personal data anywhere):

1. Candidates from Wikidata SPARQL (query.wikidata.org): newspapers, news websites, news agencies, radio and TV
   stations, news magazines, located in Spain, with an official website (P856) and no dissolution date (P576).
   Already catalogued domains, satire and pure sports/culture/science outlets are dropped.
2. Feed autodiscovery on each candidate's homepage: <link rel="alternate" type="application/rss+xml|atom+xml">,
   then a few common paths. At most 3 requests per site (homepage + 2 feed fetches; robots.txt is read once per
   host and every path is checked against it), 15 s timeout, at most 6 requests in flight and 1 per host.
3. The same inclusion gate as the catalog: the feed parses with >= 5 items, the newest item is < 7 days old,
   item links are on the outlet's own domain, the text is plausibly Spanish or co-official, the outlet has a
   documented identity (an owner/publisher in Wikidata or an es.wikipedia article), it is not an aggregator and
   not a duplicate of a catalogued outlet.
4. Output (never data/media.json):
     data/candidates.json        every candidate with status passed/failed and the reasons (machine readable)
     data/media.candidates.json  ready-to-merge media.json entries of the candidates that passed
   A human reviews them (the weekly workflow opens a pull request) and merges chosen ones with --apply.

Wikidata access reuses scripts/enrich_media.py (stdlib urllib: Wikimedia answers 403 to httpx here) and its
back-off. The long phases keep a cache in var/discover-cache.json, so a run that is interrupted or throttled
resumes where it stopped (--fresh ignores it).

Usage:
  python scripts/discover_media.py [--fresh] [--limit N] [--allow-partial]
  python scripts/discover_media.py --apply ID [ID ...]      merge chosen candidates into data/media.json
  python scripts/discover_media.py --pr-body                print the pull request body (markdown)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.parse
from collections import Counter
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import enrich_media  # noqa: E402

from app.config import settings  # noqa: E402
from app.feeds import parse_feed  # noqa: E402
from app.text import fold  # noqa: E402

DATA = ROOT / "data"
MEDIA_FILE = DATA / "media.json"
GROUPS_FILE = DATA / "groups.json"
CANDIDATES_FILE = DATA / "candidates.json"
ENTRIES_FILE = DATA / "media.candidates.json"
CACHE_FILE = ROOT / "var" / "discover-cache.json"

REPORT_VERSION = 1
UA_SPARQL = "NewsBiasAnalyzer-discover/1.0 (open-source; https://github.com/alfreedx/news-bias-analyzer)"
SPARQL_URL = "https://query.wikidata.org/sparql"
ROBOTS_TOKEN = "NewsBiasAnalyzer"  # noqa: S105 (a robots.txt product token, not a secret)

TIMEOUT = httpx.Timeout(15.0, connect=8.0)
CONCURRENCY = 6
MAX_REQUESTS_PER_SITE = 3  # homepage + 2 feed fetches (robots.txt is read once per host, apart)
MAX_HOMEPAGE_BYTES = 1024 * 1024
MAX_FEED_BYTES = 5 * 1024 * 1024
MIN_ITEMS = 5
MAX_AGE_DAYS = 7
OWN_DOMAIN_SHARE = 0.8  # share of item links that must point to the outlet's own domain
AGGREGATOR_SHARE = 0.5  # below this share the feed is an aggregator, not an outlet
SENTINEL_NOW = 2**40  # parse_feed clamps undated items to "now": this makes them recognisable

# Wikidata classes to look for (instance of, or of a subclass of, one of them).
SEARCH_TYPES = {
    "Q11032": "newspaper",
    "Q17232649": "news website",
    "Q192283": "news agency",
    "Q14350": "radio station",
    "Q1061197": "radio network",
    "Q2001305": "television channel",
    "Q1616075": "television station",
    "Q1684600": "news magazine",
}
# Same lookup, to drop: satire and subjects the catalog leaves out (sports, culture, science).
EXCLUDED_TYPES = {
    "Q19046104": "satire",
    "Q111914189": "satire",
    "Q11313190": "topic_excluded",
    "Q1791899": "topic_excluded",
    "Q5633421": "topic_excluded",
}
# Words in a name or domain that mark a purely sports / culture / science / lifestyle outlet. General news,
# regional/local news and economy stay in. Heuristic: anything it wrongly drops is a candidate for a manual add.
TOPIC_WORDS = re.compile(
    r"(?<![a-z])(deporte|deportes|deportivo|deportiva|sport|sports|futbol|baloncesto|motor|motos|golf|toros|taurino|"
    r"cine|film|musica|music|ciencia|science|cientifica|arte|artes|literatura|literario|gastronomia|cocina|moda|"
    r"videojuegos|comic|ocio|viajes|erotico|humor|satira|satirico|caballos|pesca|caza)(?![a-z])")
SATIRE_WORDS = re.compile(r"(?<![a-z])(satira|satirico|satirica|humor|parodia)(?![a-z])")

# (rel, type) of homepage <link> tags and common feed locations tried when the page does not announce one.
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml", "text/xml", "application/xml"}
COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/feed/", "/arc/outboundfeeds/rss/", "/rss/portada.xml")
# Hosts that may carry an outlet's links without being its own domain (documented CDN / redirectors). Empty on
# purpose: add "host": "reason" only when a real outlet needs it and the PR explains why.
LINK_HOST_ALLOWLIST: dict[str, str] = {}
# Two-label public suffixes used in Spain and Latin America (everything else: last two labels).
SECOND_LEVEL = {"com", "org", "gob", "nom", "edu", "co", "net"}

SPANISH_WORDS = frozenset("""de la que el en y los las del se por un para con una su al lo como mas pero sus le ya o
fue este ha si porque esta son entre cuando muy sin sobre tambien me hasta hay donde quien desde todo nos durante
todos uno les ni contra otros ese eso ante ellos e esto antes algunos unos otro otras otra tras segun tiene tienen
han ser era seran sera puede pueden hace dice""".split())
CO_OFFICIAL_WORDS = frozenset("""els les dels i amb una uns unes es son al als no mes pero tambe han hi sobre perque
davant segons fins quan aquest aquesta aquests aquestes seu seva seus seves
o a os as do da dos das unha mais tamen non nin cos coa cun cunha polo pola este esta
eta da dira du ez bat ere baina egin zen izan dute dago eta bere hau hori baino gero eskubide""".split())
ENGLISH_WORDS = frozenset("""the of and to in is that for it with as was on are by this be at from or an have has
not but they their will would there been were which more about after over said new""".split())

IDENTITY_KINDS = {  # direct P31 values -> media.json "type", in priority order
    "Q192283": "Agencia", "Q2001305": "Televisión", "Q1616075": "Televisión", "Q14350": "Radio",
    "Q1061197": "Radio", "Q41298": "Revista", "Q1684600": "Revista", "Q1110794": "Diario", "Q11032": "Diario",
    "Q1153191": "Digital", "Q17232649": "Digital", "Q2024496": "Digital",
}
QUERY_TYPE_TO_TYPE = {
    "Q192283": "Agencia", "Q2001305": "Televisión", "Q1616075": "Televisión", "Q14350": "Radio",
    "Q1061197": "Radio", "Q1684600": "Revista", "Q11032": "Diario", "Q17232649": "Digital",
}
REGIONS = {  # Wikidata (es) label of an autonomous community -> the name used in media.json
    "Cataluña": "Cataluña", "Galicia": "Galicia", "País Vasco": "País Vasco", "Andalucía": "Andalucía",
    "Comunidad Valenciana": "Comunidad Valenciana", "Aragón": "Aragón", "Comunidad Foral de Navarra": "Navarra",
    "Navarra": "Navarra", "Principado de Asturias": "Asturias", "Asturias": "Asturias", "Cantabria": "Cantabria",
    "Extremadura": "Extremadura", "Canarias": "Canarias", "Región de Murcia": "Región de Murcia",
    "Castilla-La Mancha": "Castilla-La Mancha", "Castilla y León": "Castilla y León",
    "Islas Baleares": "Islas Baleares", "Illes Balears": "Islas Baleares", "La Rioja": "La Rioja",
    "Comunidad de Madrid": None,  # Madrid hosts national outlets: no region
}

FAILURE_TEXT = {
    "already_in_catalog": "ya está en el catálogo", "satire": "sátira", "topic_excluded": "temática fuera del catálogo",
    "no_homepage": "sin web utilizable", "robots_disallowed": "robots.txt no lo permite",
    "robots_unavailable": "robots.txt no disponible", "homepage_unreachable": "la portada no responde",
    "no_feed_found": "sin feed", "feed_unreachable": "el feed no responde", "feed_not_parseable": "el feed no se lee",
    "feed_not_https": "feed sin https", "feed_too_few_items": "menos de 5 titulares",
    "no_item_dates": "titulares sin fecha", "feed_stale": "sin titulares de la última semana",
    "links_off_domain": "enlaces fuera de su dominio", "aggregator": "agregador", "language_not_spanish": "idioma",
    "not_checked": "no comprobado en esta ejecución", "wikidata_unavailable": "Wikidata no respondió",
    "no_identity": "sin propietario ni artículo en Wikipedia", "duplicate_candidate": "duplicado de otro candidato",
}


# --- small pure helpers (unit-tested) ----------------------------------------------------------------
def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def domain_of(url: str) -> str:
    """Host without a leading «www.» (the form used in media.json)."""
    return host_of(url).removeprefix("www.")


def registered_domain(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def on_domain(host: str, domains: set[str]) -> bool:
    """True when ``host`` is one of the registered domains or a subdomain of one."""
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in domains) or host in LINK_HOST_ALLOWLIST


def is_public_host(host: str) -> bool:
    """Refuse IP literals, localhost and single-label names: candidates come from a public wiki, not a trusted list."""
    if not host or "." not in host or host == "localhost":
        return False
    return not re.fullmatch(r"[\d.]+|[0-9a-f:]+", host) and not host.endswith((".local", ".internal", ".lan"))


def slugify(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", fold(name))).strip("-")


def text_tokens(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", fold(text))


def language_scores(texts: list[str]) -> tuple[float, float]:
    """(share of Spanish or co-official stopwords, share of English stopwords) over all words of ``texts``."""
    tokens = [t for text in texts for t in text_tokens(text)]
    if not tokens:
        return 0.0, 0.0
    es = sum(t in SPANISH_WORDS or t in CO_OFFICIAL_WORDS for t in tokens)
    en = sum(t in ENGLISH_WORDS for t in tokens)
    return es / len(tokens), en / len(tokens)


def language_ok(texts: list[str]) -> bool:
    es, en = language_scores(texts)
    return es >= 0.12 and es >= 1.5 * en


class _LinkFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            d = {k.lower(): (v or "") for k, v in attrs}
            if "alternate" in d.get("rel", "").lower().split() and d.get("href"):
                self.links.append(d)


def find_feed_links(html: str, base: str) -> list[str]:
    """Feed URLs announced by <link rel="alternate" type="application/rss+xml|atom+xml">, best first.

    Comment feeds rank last. Only http(s) URLs are returned; duplicates are removed.
    """
    parser = _LinkFinder()
    try:
        parser.feed(html[:400_000])
    except Exception:  # noqa: BLE001  (malformed HTML must never stop discovery)
        return []
    scored: list[tuple[int, int, str]] = []
    for i, link in enumerate(parser.links):
        if link.get("type", "").lower().split(";")[0].strip() not in FEED_TYPES:
            continue
        url = urljoin(base, link["href"].strip())
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        label = fold(link.get("title", "") + " " + urlsplit(url).path)
        scored.append((1 if re.search(r"comment|comentario|opinion", label) else 0, i, url))
    seen: dict[str, None] = {}
    for _, _, url in sorted(scored):
        seen.setdefault(url, None)
    return list(seen)


def candidate_feed_urls(html: str | None, base: str, limit: int = MAX_REQUESTS_PER_SITE - 1) -> list[str]:
    """The feed URLs to try, in order: announced by the page first, then the common paths. At most ``limit``."""
    urls = find_feed_links(html, base) if html else []
    origin = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    for path in COMMON_FEED_PATHS:
        urls.append(origin + path)
    return list(dict.fromkeys(urls))[:limit]


def validate_feed(content: bytes, domains: set[str], now: int) -> dict[str, Any]:
    """Apply the catalog's technical gate to one feed body. Returns {"reasons": [...], "items": n, ...}."""
    items = parse_feed(content, now=SENTINEL_NOW)
    result: dict[str, Any] = {"reasons": [], "items": len(items)}
    if not items:
        result["reasons"].append("feed_not_parseable")
        return result
    if len(items) < MIN_ITEMS:
        result["reasons"].append("feed_too_few_items")
    dated = [i.published for i in items if i.published != SENTINEL_NOW]
    if not dated:
        result["reasons"].append("no_item_dates")
    else:
        newest = max(dated)
        result["newest"] = datetime.fromtimestamp(min(newest, now), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if now - newest > MAX_AGE_DAYS * 86400:
            result["reasons"].append("feed_stale")
    own = sum(on_domain(host_of(i.url), domains) for i in items)
    share = own / len(items)
    result["own_domain_share"] = round(share, 2)
    if share < AGGREGATOR_SHARE:
        result["reasons"].append("aggregator")
    elif share < OWN_DOMAIN_SHARE:
        result["reasons"].append("links_off_domain")
    sample = [f"{i.title} {i.summary}" for i in items[:30]]
    es, en = language_scores(sample)
    result["spanish_share"], result["english_share"] = round(es, 3), round(en, 3)
    if not language_ok(sample):
        result["reasons"].append("language_not_spanish")
    return result


def parse_bindings(bindings: list[dict], query_type: str) -> dict[str, dict]:
    """SPARQL result rows -> {qid: candidate}. One item can come with several websites and labels."""
    out: dict[str, dict] = {}
    for row in bindings:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        if not re.fullmatch(r"Q\d+", qid):
            continue
        c = out.setdefault(qid, {"qid": qid, "name": None, "websites": [], "inception": None, "es_wikipedia": None,
                                 "query_types": []})
        label = row.get("itemLabel", {}).get("value")
        if label and not re.fullmatch(r"Q\d+", label):
            c["name"] = c["name"] or label
        site = row.get("site", {}).get("value")
        if site and site not in c["websites"]:
            c["websites"].append(site)
        year = re.match(r"^\+?(\d{4})", row.get("inception", {}).get("value", ""))
        if year and not c["inception"]:
            c["inception"] = int(year.group(1))
        article = row.get("es", {}).get("value")
        if article and not c["es_wikipedia"]:
            c["es_wikipedia"] = article
        if query_type not in c["query_types"]:
            c["query_types"].append(query_type)
    return out


def build_query(type_qid: str, details: bool = True) -> str:
    extra = ""
    sel = "?item"
    if details:
        sel = "?item ?itemLabel ?site ?inception ?es"
        extra = (
            "?item wdt:P856 ?site.\n"
            "  OPTIONAL { ?item wdt:P571 ?inception. }\n"
            "  OPTIONAL { ?es schema:about ?item; schema:isPartOf <https://es.wikipedia.org/>. }\n"
            "  FILTER NOT EXISTS { ?item wdt:P576 ?dissolved. }\n"
            '  SERVICE wikibase:label { bd:serviceParam wikibase:language "es,ca,gl,eu,en". }'
        )
    return (f"SELECT DISTINCT {sel} WHERE {{\n"
            f"  ?item wdt:P31/wdt:P279* wd:{type_qid}.\n"
            "  { ?item wdt:P17 wd:Q29. } UNION { ?item wdt:P291 ?place. ?place wdt:P17 wd:Q29. }\n"
            f"  {extra}\n}}")


def exclusion_reason(c: dict, catalog_domains: set[str], catalog_qids: set[str],
                     satire_qids: set[str], topic_qids: set[str]) -> str | None:
    """Why a candidate is dropped before any feed is fetched (None = keep)."""
    if c["qid"] in catalog_qids:
        return "already_in_catalog"
    hosts = {domain_of(w) for w in c["websites"] if is_public_host(host_of(w))}
    if not hosts:
        return "no_homepage"
    for h in hosts:
        if on_domain(h, catalog_domains) or any(d.endswith("." + h) for d in catalog_domains):
            return "already_in_catalog"
    words = fold((c["name"] or "") + " " + " ".join(sorted(hosts)).replace(".", " ").replace("-", " "))
    if c["qid"] in satire_qids or SATIRE_WORDS.search(words):
        return "satire"
    if c["qid"] in topic_qids or TOPIC_WORDS.search(words):
        return "topic_excluded"
    return None


def guess_type(kinds_qids: list[str], query_types: list[str]) -> str:
    for q, label in IDENTITY_KINDS.items():
        if q in kinds_qids:
            return label
    for q, label in QUERY_TYPE_TO_TYPE.items():
        if q in query_types:
            return label
    return "Digital"


def group_lookup(groups: list[dict], media: list[dict]) -> dict[str, str]:
    """Map «owner key» -> group id. Keys: folded group names/ids and Wikidata QIDs of the owners of catalogued
    outlets that belong to the group and carry the group's name (a co-owner never gives its QID to the group)."""
    keys: dict[str, str] = {}
    for g in groups:
        keys[fold(g["name"])] = g["id"]
        keys[fold(g["id"].replace("-", " "))] = g["id"]
        for s in g.get("sources", []):
            m = re.search(r"wikidata\.org/wiki/(Q\d+)", s.get("url", ""))
            if m and "propiedad" not in s.get("title", "").lower():
                keys.setdefault(m.group(1), g["id"])
    names = {g["id"]: fold(g["name"]) for g in groups}
    for m in media:
        gid, facts = m.get("group"), m.get("facts", {})
        for q, label in zip(facts.get("owners_qids", []), facts.get("owners", []), strict=False):
            if gid and fold(label) and (fold(label) in names[gid] or names[gid] in fold(label)):
                keys.setdefault(q, gid)
    return keys


def owner_group(owners: list[tuple[str, str]], lookup: dict[str, str]) -> str | None:
    """Group id when one of the Wikidata owners/publishers (qid, label) is a registered group, else None."""
    for qid, label in owners:
        if qid in lookup:
            return lookup[qid]
        if fold(label) in lookup:
            return lookup[fold(label)]
    return None


def build_entry(c: dict, facts: dict, region: str | None, lookup: dict[str, str], used_ids: set[str]) -> dict:
    """A media.json entry for a candidate that passed. ``evidence`` stays empty: ideology labels only ever come
    from a cited external source, and nobody has cited one for this outlet yet."""
    wikidata = f"https://www.wikidata.org/wiki/{c['qid']}"
    base = slugify(c["name"]) or c["qid"].lower()
    mid = base if base not in used_ids else f"{base}-{c['qid'].lower()}"
    owners = list(zip(facts.get("owners_qids", []), facts.get("owners", []), strict=False))
    group = owner_group(owners, lookup)
    entry: dict[str, Any] = {
        "id": mid, "name": c["name"], "domains": [c["domain"]],
        "type": guess_type(facts.get("kinds_qids", []), c["query_types"]),
        "scope": "Regional" if region else "Nacional",
    }
    if region:
        entry["region"] = region
    founded = facts.get("founded") or c.get("inception")
    entry["founded"] = founded
    entry["group"] = group
    if owners and not group:
        entry["publisher"] = owners[0][1]
    if owners:
        entry["owner_source"] = wikidata
    entry["feeds"] = [c["feed"]]
    entry["evidence"] = []
    if facts:
        entry["facts"] = facts
    return entry


# --- Wikidata (stdlib urllib, polite back-off) ---------------------------------------------------------
def sparql(query: str, attempts: int = 6) -> list[dict] | None:
    """Run a SPARQL query. Honours Retry-After on 429/503, backs off exponentially, None when it never worked."""
    url = f"{SPARQL_URL}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
    req = Request(url, headers={"User-Agent": UA_SPARQL, "Accept": "application/sparql-results+json"})  # noqa: S310
    for attempt in range(attempts):
        wait = 10 * 2**attempt
        try:
            with urlopen(req, timeout=90) as r:  # noqa: S310 (fixed https host)
                return json.load(r)["results"]["bindings"]
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                print(f"  SPARQL HTTP {exc.code}", flush=True)
                return None
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            if retry_after.isdigit():
                wait = max(wait, min(int(retry_after), 300))
            status = exc.code
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            status = type(exc).__name__
        print(f"  SPARQL retry {attempt + 1}/{attempts} ({status}) in {min(wait, 300)} s", flush=True)
        time.sleep(min(wait, 300))
    return None


def regions_for(entities: dict[str, dict]) -> dict[str, str | None]:
    """qid -> autonomous community of the outlet's place of publication (P291) or headquarters (P159), found by
    following «located in the administrative territory» (P131) upwards. None for Madrid (home of national outlets);
    a qid is absent when Wikidata does not say. Uses the cheap entity API, not SPARQL (which times out on P131*)."""
    out: dict[str, str | None] = {}
    pending = {q: (enrich_media.item_ids(e, "P291") + enrich_media.item_ids(e, "P159"))[:4]
               for q, e in entities.items() if "missing" not in e}
    for _ in range(6):
        wanted = sorted({p for places in pending.values() for p in places})
        if not wanted:
            break
        places = enrich_media.fetch_entities(wanted, "claims|labels")
        following: dict[str, list[str]] = {}
        for q, ids in pending.items():
            up: list[str] = []
            for pid in ids:
                ent = places.get(pid)
                if not ent or "missing" in ent:
                    continue
                labels = ent.get("labels", {})
                label = (labels.get("es") or labels.get("en") or {}).get("value", "")
                if label in REGIONS:
                    out[q] = REGIONS[label]
                    up = []
                    break
                up += enrich_media.item_ids(ent, "P131")
            if q not in out and up:
                following[q] = list(dict.fromkeys(up))[:4]
        pending = following
    return out


# --- fetching ----------------------------------------------------------------------------------------
class Fetched:
    __slots__ = ("body", "final_url", "status")

    def __init__(self, status: int | None, body: bytes = b"", final_url: str = ""):
        self.status, self.body, self.final_url = status, body, final_url


async def fetch(client: httpx.AsyncClient, url: str, cap: int, truncate: bool = False) -> Fetched:
    """GET with a size cap (``truncate``: keep the first ``cap`` bytes instead of failing). Never raises."""
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return Fetched(resp.status_code, b"", str(resp.url))
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                chunks.append(chunk)
                if size > cap:
                    if not truncate:
                        return Fetched(None)
                    break
            return Fetched(200, b"".join(chunks)[:cap], str(resp.url))
    except (httpx.HTTPError, ValueError, OSError):
        return Fetched(None)


class Robots:
    """robots.txt per origin, read once, checked for the project's product token (falls back to «*»)."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cache: dict[str, RobotFileParser | str] = {}

    async def allowed(self, url: str) -> bool | None:
        """True / False, or None when robots.txt could not be read (5xx or network: be conservative)."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.cache:
            res = await fetch(self.client, origin + "/robots.txt", 512 * 1024, truncate=True)
            self.cache[origin] = self.parse(res.status, res.body)
        rules = self.cache[origin]
        if rules == "allow":
            return True
        if rules == "deny":
            return False
        if rules == "unknown":
            return None
        return rules.can_fetch(ROBOTS_TOKEN, url)  # type: ignore[union-attr]

    @staticmethod
    def parse(status: int | None, body: bytes) -> RobotFileParser | str:
        if status == 200:
            rp = RobotFileParser()
            rp.parse(body.decode("utf-8", "replace").splitlines())
            return rp
        if status in (401, 403):
            return "deny"
        if status is not None and 400 <= status < 500:
            return "allow"
        return "unknown"


async def check_site(client: httpx.AsyncClient, robots: Robots, c: dict, now: int) -> dict:
    """Autodiscover and validate the feed of one candidate. At most MAX_REQUESTS_PER_SITE content requests."""
    result: dict[str, Any] = {"reasons": [], "requests": 0}
    home = next((w for w in c["websites"] if is_public_host(host_of(w))), None)
    if home is None:
        result["reasons"].append("no_homepage")
        return result
    domains = {registered_domain(host_of(w)) for w in c["websites"] if is_public_host(host_of(w))}
    verdict = await robots.allowed(home)
    if verdict is not True:
        result["reasons"].append("robots_unavailable" if verdict is None else "robots_disallowed")
        return result
    page = await fetch(client, home, MAX_HOMEPAGE_BYTES, truncate=True)
    result["requests"] += 1
    if page.status != 200:
        result["reasons"].append("homepage_unreachable")
        result["homepage_status"] = page.status
        return result
    domains.add(registered_domain(host_of(page.final_url)))
    html = page.body.decode("utf-8", "replace")
    tried, last = [], None
    for url in candidate_feed_urls(html, page.final_url):
        if result["requests"] >= MAX_REQUESTS_PER_SITE:
            break
        if not is_public_host(host_of(url)) or await robots.allowed(url) is not True:
            continue
        got = await fetch(client, url, MAX_FEED_BYTES)
        result["requests"] += 1
        tried.append(url)
        if got.status != 200:
            last = "feed_unreachable"
            continue
        verdict = validate_feed(got.body, domains, now)
        if "feed_not_parseable" in verdict["reasons"]:
            last = "feed_not_parseable"
            continue
        final = got.final_url or url
        if urlsplit(final).scheme != "https":
            verdict["reasons"].append("feed_not_https")
        result.update(feed=final, **{k: v for k, v in verdict.items() if k != "reasons"})
        result["reasons"].extend(verdict["reasons"])
        return result
    result["tried"] = tried
    result["reasons"].append(last or "no_feed_found")
    return result


async def check_all(candidates: list[dict], cache: dict, now: int, save) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    hosts: dict[str, asyncio.Semaphore] = {}
    done = 0
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, max_redirects=5,
                                 headers={"User-Agent": settings.user_agent,
                                          "Accept": "application/rss+xml, application/atom+xml, text/html;q=0.8, "
                                                    "*/*;q=0.5"}) as client:
        robots = Robots(client)

        async def one(c: dict) -> None:
            nonlocal done
            host = domain_of(c["websites"][0]) if c["websites"] else ""
            async with hosts.setdefault(host, asyncio.Semaphore(1)), sem:
                try:
                    cache["checks"][c["qid"]] = await check_site(client, robots, c, now)
                except Exception as exc:  # noqa: BLE001  (one odd site must not stop the run)
                    cache["checks"][c["qid"]] = {"reasons": ["homepage_unreachable"], "error": type(exc).__name__}
            done += 1
            if done % 25 == 0:
                save()
                print(f"  {done}/{len(candidates)} sites checked", flush=True)

        await asyncio.gather(*(one(c) for c in candidates))
    save()


# --- files -------------------------------------------------------------------------------------------
def dumps(doc: dict) -> bytes:
    """Same byte layout as the hand-maintained data files (LF, indent 1, no trailing newline)."""
    return json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def strip_dates(value: Any) -> Any:
    """For comparing runs: ``retrieved`` dates change every week and must not alone make a pull request."""
    if isinstance(value, dict):
        return {k: strip_dates(v) for k, v in value.items() if k != "retrieved"}
    if isinstance(value, list):
        return [strip_dates(v) for v in value]
    return value


def stabilise(old_entries: list[dict], new_entries: list[dict]) -> list[dict]:
    """Keep the previous entry (and its retrieval date) when only the date would differ."""
    old = {e["id"]: e for e in old_entries}
    return [old[e["id"]] if e["id"] in old and strip_dates(old[e["id"]]) == strip_dates(e) else e
            for e in new_entries]


def catalog_index(media: list[dict]) -> tuple[set[str], set[str]]:
    domains = {d.lower().removeprefix("www.") for m in media for d in m["domains"]}
    qids = {m["facts"]["qid"] for m in media if m.get("facts", {}).get("qid")}
    return domains, qids


# --- pull request body (unit-tested) ---------------------------------------------------------------------
def render_pr_body(entries_doc: dict, report: dict | None = None) -> str:
    entries = entries_doc.get("media", [])
    by_id = {c["id"]: c for c in (report or {}).get("candidates", []) if c.get("status") == "passed"}
    lines = [
        "Candidatos a medio encontrados automáticamente (Wikidata + feeds públicos). "
        "**Ninguno está en el catálogo todavía**: pasan la puerta técnica y de identidad, pero un humano decide.",
        "",
        f"Candidatos que pasan la validación: **{len(entries)}**.",
        "",
    ]
    if entries:
        lines += ["| Medio | Dominio | Feed | Propietario | Fuente |", "|---|---|---|---|---|"]
    for e in entries:
        info = by_id.get(e["id"], {})
        owners = e.get("facts", {}).get("owners") or []
        owner = ", ".join(owners) if owners else "sin propietario en Wikidata"
        source = e.get("owner_source") or info.get("identity_source") or e.get("facts", {}).get("source", "")
        cells = [e["name"], e["domains"][0], e["feeds"][0], owner, f"[enlace]({source})" if source else "—"]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    lines += [
        "", "### Lista de revisión", "",
        "- [ ] Abrir cada feed y comprobar que son noticias del propio medio y no un agregador.",
        "- [ ] Confirmar la identidad (propietario o artículo de Wikipedia) en el enlace de la fuente.",
        "- [ ] Descartar sátira, contenido solo deportivo/cultural o medios que pidan no ser indexados.",
        "- [ ] Revisar `type`, `scope`, `region` y `group` (se derivan de Wikidata y pueden ser inexactos).",
        "- [ ] Dejar `evidence` vacío salvo que exista una fuente citable de orientación.",
        "- [ ] Dar de alta los elegidos: `python scripts/discover_media.py --apply ID [ID ...]`, "
        "y luego `python -m pytest`.",
        "",
        "El informe completo (también los descartados y sus motivos) está en `data/candidates.json`.",
    ]
    return "\n".join(lines) + "\n"


# --- --apply -------------------------------------------------------------------------------------------
def apply_candidates(ids: list[str], media_file: Path = MEDIA_FILE, entries_file: Path = ENTRIES_FILE,
                     groups_file: Path = GROUPS_FILE) -> list[str]:
    """Merge the chosen entries of media.candidates.json into media.json (same byte layout), after the last news
    outlet and in alphabetical order of id. Nothing is written when any request is invalid. Returns the ids added."""
    doc = json.loads(media_file.read_text(encoding="utf-8"))
    entries = {e["id"]: e for e in json.loads(entries_file.read_text(encoding="utf-8"))["media"]}
    groups = {g["id"] for g in json.loads(groups_file.read_text(encoding="utf-8"))["groups"]}
    media = doc["media"]
    taken_ids = {m["id"] for m in media}
    taken_domains = {d for m in media for d in m["domains"]}
    errors, chosen = [], []
    for mid in dict.fromkeys(ids):
        e = entries.get(mid)
        if e is None:
            errors.append(f"{mid}: no está en {entries_file.name}")
        elif mid in taken_ids:
            errors.append(f"{mid}: ya existe en {media_file.name}")
        elif set(e["domains"]) & taken_domains:
            errors.append(f"{mid}: su dominio ya está en el catálogo")
        elif e.get("group") and e["group"] not in groups:
            errors.append(f"{mid}: el grupo {e['group']!r} no existe en groups.json")
        else:
            chosen.append(e)
    if errors:
        raise SystemExit("No se ha cambiado nada:\n  " + "\n  ".join(errors))
    last_news = max(i for i, m in enumerate(media) if not m.get("factchecker"))
    chosen.sort(key=lambda e: e["id"])
    media[last_news + 1:last_news + 1] = chosen
    media_file.write_bytes(dumps(doc))
    return [e["id"] for e in chosen]


# --- main ----------------------------------------------------------------------------------------------
def run(fresh: bool, limit: int | None, allow_partial: bool) -> int:
    now = int(time.time())
    media_doc = load_json(MEDIA_FILE)
    groups_doc = load_json(GROUPS_FILE)
    media = media_doc["media"]
    catalog_domains, catalog_qids = catalog_index(media)
    cache = {} if fresh else load_json(CACHE_FILE, {})
    if cache.get("day") != date.today().isoformat():
        cache = {}  # a cache is only good for the day it was made: results age quickly
    cache.setdefault("day", date.today().isoformat())
    for key in ("sparql", "excluded", "checks"):
        cache.setdefault(key, {})

    def save() -> None:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 1. Wikidata candidates
    failed_types = []
    for qid, label in SEARCH_TYPES.items():
        if qid in cache["sparql"]:
            continue
        print(f"SPARQL {label} ({qid})", flush=True)
        rows = sparql(build_query(qid))
        if rows is None:
            failed_types.append(qid)
            continue
        cache["sparql"][qid] = rows
        save()
        time.sleep(3)
    for qid, label in EXCLUDED_TYPES.items():
        if qid in cache["excluded"]:
            continue
        print(f"SPARQL exclusion {label} ({qid})", flush=True)
        rows = sparql(build_query(qid, details=False))
        if rows is None:
            failed_types.append(qid)
            continue
        cache["excluded"][qid] = [r["item"]["value"].rsplit("/", 1)[-1] for r in rows]
        save()
        time.sleep(3)
    if failed_types and not allow_partial:
        print(f"Wikidata did not answer for {failed_types}; nothing written (run again to resume, "
              "or --allow-partial).", file=sys.stderr)
        return 2

    found: dict[str, dict] = {}
    for qid, rows in cache["sparql"].items():
        for q, c in parse_bindings(rows, qid).items():
            if q in found:
                f = found[q]
                f["websites"] += [w for w in c["websites"] if w not in f["websites"]]
                f["query_types"] += [t for t in c["query_types"] if t not in f["query_types"]]
                for k in ("name", "inception", "es_wikipedia"):
                    f[k] = f[k] or c[k]
            else:
                found[q] = c
    satire = {q for t, ids in cache["excluded"].items() if EXCLUDED_TYPES.get(t) == "satire" for q in ids}
    topics = {q for t, ids in cache["excluded"].items() if EXCLUDED_TYPES.get(t) == "topic_excluded" for q in ids}

    report: dict[str, dict] = {}
    pending: list[dict] = []
    for q in sorted(found, key=lambda q: int(q[1:])):
        c = found[q]
        c["name"] = c["name"] or q
        c["websites"] = sorted(set(c["websites"]))
        c["domain"] = domain_of(c["websites"][0]) if c["websites"] else ""
        reason = exclusion_reason(c, catalog_domains, catalog_qids, satire, topics)
        report[q] = {"qid": q, "name": c["name"], "website": c["websites"][0] if c["websites"] else None,
                     "domain": c["domain"], "status": "failed", "reasons": [reason] if reason else [],
                     "es_wikipedia": c["es_wikipedia"]}
        if not reason:
            pending.append(c)
    print(f"{len(found)} candidates from Wikidata; {len(pending)} left after the pre-filter", flush=True)

    # 2-3. feed autodiscovery and validation
    todo = [c for c in pending if c["qid"] not in cache["checks"]][: limit or None]
    if todo:
        print(f"checking {len(todo)} sites (cache holds {len(cache['checks'])})", flush=True)
        asyncio.run(check_all(todo, cache, now, save))
    checked = [c for c in pending if c["qid"] in cache["checks"]]
    for c in pending:
        if c["qid"] not in cache["checks"]:
            report[c["qid"]]["reasons"] = ["not_checked"]

    # a candidate whose domain repeats another candidate's is kept once (the one with an es.wikipedia article first)
    seen: dict[str, str] = {}
    for c in sorted(checked, key=lambda c: (c["es_wikipedia"] is None, int(c["qid"][1:]))):
        rd = registered_domain(c["domain"])
        if rd in seen:
            cache["checks"][c["qid"]] = {**cache["checks"][c["qid"]], "dup_of": seen[rd]}
        elif not cache["checks"][c["qid"]]["reasons"]:
            seen[rd] = c["qid"]

    technical_ok = []
    for c in checked:
        chk = cache["checks"][c["qid"]]
        reasons = list(chk["reasons"])
        if chk.get("dup_of"):
            reasons.append("duplicate_candidate")
        report[c["qid"]].update({k: chk[k] for k in ("feed", "items", "newest", "own_domain_share", "spanish_share",
                                                    "requests") if k in chk})
        report[c["qid"]]["reasons"] = reasons
        if not reasons:
            technical_ok.append(c)

    # 4. identity (Wikidata owner/publisher or es.wikipedia article) and facts, for the survivors only.
    # Answers are cached per outlet; a batch that Wikidata throttled is never trusted (missing facts would read
    # as "no owner"), so an incomplete run stops here and the next one resumes.
    entries: list[dict] = []
    wikidata = cache.setdefault("wikidata", {})
    todo_facts = [c["qid"] for c in technical_ok if c["qid"] not in wikidata]
    if technical_ok:
        print(f"{len(technical_ok)} sites pass the feed checks; reading Wikidata facts for {len(todo_facts)}",
              flush=True)
    for batch in enrich_media.chunks(todo_facts, 30):
        before = enrich_media.FAILED_BATCHES
        entities, facts_by_qid = enrich_media.fetch_current(batch)
        regions = regions_for({q: entities[q] for q in batch if q in entities})
        if before != enrich_media.FAILED_BATCHES:
            print("  Wikidata throttled this batch; it will be retried on the next run", flush=True)
            time.sleep(30)
            continue
        for q in batch:
            wikidata[q] = {"facts": facts_by_qid.get(q, {}), "region": regions.get(q)}
        save()
    missing = {c["qid"] for c in technical_ok if c["qid"] not in wikidata}
    if missing and not allow_partial:
        print(f"Wikidata facts missing for {len(missing)} outlets (throttled); nothing written. "
              "Run again to resume.", file=sys.stderr)
        return 3
    if technical_ok:
        lookup = group_lookup(groups_doc["groups"], media)
        used = {m["id"] for m in media}
        for c in technical_ok:
            if c["qid"] in missing:
                report[c["qid"]]["reasons"] = ["wikidata_unavailable"]
                continue
            facts = wikidata[c["qid"]]["facts"]
            owners = facts.get("owners", [])
            if not owners and not c["es_wikipedia"]:
                report[c["qid"]]["reasons"] = ["no_identity"]
                continue
            c["feed"] = cache["checks"][c["qid"]]["feed"]
            entry = build_entry(c, facts, wikidata[c["qid"]]["region"], lookup, used)
            used.add(entry["id"])
            entries.append(entry)
            r = report[c["qid"]]
            r.update(status="passed", id=entry["id"],
                     identity={"owners": owners, "es_wikipedia": c["es_wikipedia"]},
                     identity_source=entry.get("owner_source") or c["es_wikipedia"])
    entries.sort(key=lambda e: e["id"])

    # 5. write (entries keep their previous retrieval date when nothing else changed)
    old_entries = (load_json(ENTRIES_FILE, {}) or {}).get("media", [])
    entries = stabilise(old_entries, entries)
    entries_doc = {"_meta": {
        "description": "Medios candidatos generados por scripts/discover_media.py. NO forman parte del catálogo: "
                       "un humano los revisa y los elegidos se añaden con --apply. Informe completo: candidates.json.",
        "license": "CC BY-SA 4.0"}, "media": entries}
    ordered = sorted(report.values(), key=lambda r: (r["status"] != "passed", r["name"].lower(), r["qid"]))
    histogram = Counter(reason for r in report.values() if r["status"] == "failed" for reason in r["reasons"][:1])
    report_doc = {"_meta": {
        "version": REPORT_VERSION, "generated": date.today().isoformat(),
        "description": "Informe de scripts/discover_media.py: todos los candidatos de Wikidata y el resultado de la "
                       "validación (passed/failed y motivos).",
        "sources": ["https://www.wikidata.org", "feeds RSS/Atom públicos de cada medio"],
        "license": "CC BY-SA 4.0",
        "counts": {"sparql_candidates": len(found), "checked_sites": len(checked),
                   "with_feed": sum(1 for r in report.values() if r.get("feed")),
                   "passed": len(entries), "failed": len(report) - len(entries),
                   "failure_reasons": dict(histogram.most_common())}},
        "candidates": ordered}
    ENTRIES_FILE.write_bytes(dumps(entries_doc))
    CANDIDATES_FILE.write_bytes(dumps(report_doc))
    print_summary(report_doc, entries)
    return 0


def print_summary(report_doc: dict, entries: list[dict]) -> None:
    counts = report_doc["_meta"]["counts"]
    print(f"\nSPARQL candidates: {counts['sparql_candidates']}\nSites checked:     {counts['checked_sites']}\n"
          f"With a valid feed: {counts['with_feed']}\nPassed:            {counts['passed']}\n"
          f"Failed:            {counts['failed']}\nFailure reasons (first reason of each):")
    for reason, n in counts["failure_reasons"].items():
        print(f"  {n:5d}  {reason}  ({FAILURE_TEXT.get(reason, '')})")
    print("\nSample of passed candidates:")
    for e in entries[:15]:
        print(f"  {e['id']:32s} {e['domains'][0]:30s} {e['type']:10s} {e['scope']:8s} group={e['group']}")


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", nargs="+", metavar="ID", help="merge these candidates into data/media.json")
    ap.add_argument("--pr-body", action="store_true", help="print the pull request body and exit")
    ap.add_argument("--fresh", action="store_true", help="ignore the resume cache")
    ap.add_argument("--limit", type=int, help="check at most N new sites this run (development)")
    ap.add_argument("--allow-partial", action="store_true", help="write results even if some SPARQL query failed")
    args = ap.parse_args(argv)
    if args.apply:
        added = apply_candidates(args.apply)
        print("Añadidos a data/media.json: " + ", ".join(added))
        print("Ahora: python -m pytest   (y revisa type, scope, region y group a mano)")
        return 0
    if args.pr_body:
        sys.stdout.write(render_pr_body(load_json(ENTRIES_FILE, {"media": []}), load_json(CANDIDATES_FILE, {})))
        return 0
    return run(args.fresh, args.limit, args.allow_partial)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
