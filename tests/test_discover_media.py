"""Discovery rules on fixtures. No network: the HTTP layer is an httpx MockTransport."""

import asyncio
import json
import shutil
import sys
import time
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import discover_media as dm  # noqa: E402

from app.catalog import Catalog  # noqa: E402

NOW = int(time.time())
DOMAINS = {"diariodemo.es"}
TITLES = ["El Ayuntamiento aprueba el presupuesto de la ciudad para el año que viene",
          "La lluvia llega a la región con más fuerza de lo previsto por los meteorólogos",
          "Los vecinos del barrio protestan por el cierre del centro de salud",
          "El equipo local gana el partido en la última jornada de la liga",
          "La Policía detiene a un hombre por el robo en una joyería del centro",
          "El precio de la vivienda sube en la provincia durante el último trimestre"]


def rss(n=6, host="www.diariodemo.es", age_days=0.0, dated=True, titles=None, scheme="https"):
    items = []
    for i in range(n):
        title = (titles or TITLES)[i % len(titles or TITLES)]
        date = f"<pubDate>{formatdate(NOW - int(age_days * 86400) - i * 600)}</pubDate>" if dated else ""
        items.append(f"<item><title>{title}</title><link>{scheme}://{host}/n/{i}</link>{date}"
                     f"<description>Resumen de la noticia número {i} que cuenta lo que ha pasado en la zona"
                     "</description></item>")
    return ("<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>" + "".join(items)
            + "</channel></rss>").encode()


def test_good_feed_passes():
    r = dm.validate_feed(rss(), DOMAINS, NOW)
    assert r["reasons"] == [] and r["items"] == 6 and r["own_domain_share"] == 1.0


@pytest.mark.parametrize("kwargs,reason", [
    ({"n": 4}, "feed_too_few_items"),
    ({"age_days": 9}, "feed_stale"),
    ({"dated": False}, "no_item_dates"),
    ({"host": "otro-sitio.com"}, "aggregator"),
    ({"titles": ["The government said that the new law will be one of the most important for the country "
                 "and there are many people who have been waiting for it"]}, "language_not_spanish"),
])
def test_feed_rules(kwargs, reason):
    assert reason in dm.validate_feed(rss(**kwargs), DOMAINS, NOW)["reasons"]


def test_partly_foreign_links_are_not_an_aggregator_but_fail():
    body = rss(10)
    for i in (0, 1, 2):  # 3 of 10 links elsewhere: 70 % own domain
        body = body.replace(f"https://www.diariodemo.es/n/{i}<".encode(), f"https://elsewhere.example/n/{i}<".encode())
    reasons = dm.validate_feed(body, DOMAINS, NOW)["reasons"]
    assert "links_off_domain" in reasons and "aggregator" not in reasons


def test_subdomains_count_as_own_domain_and_lookalikes_do_not():
    assert dm.on_domain("feeds.diariodemo.es", DOMAINS)
    assert not dm.on_domain("diariodemo.es.evil.example", DOMAINS)
    assert not dm.on_domain("xdiariodemo.es", DOMAINS)


def test_unparseable_feed():
    assert dm.validate_feed(b"<html>not a feed</html>", DOMAINS, NOW)["reasons"] == ["feed_not_parseable"]
    xxe = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item>'
           b"<title>&x;</title><link>https://a.es</link></item></channel></rss>")
    assert dm.validate_feed(xxe, DOMAINS, NOW)["reasons"] == ["feed_not_parseable"]


def test_language_check_accepts_co_official_languages_and_rejects_english():
    ca = ["El Parlament aprova els pressupostos amb el vot dels partits i els sindicats no ho accepten"]
    gl = ["O Parlamento aproba os orzamentos co voto dos partidos e os sindicatos non o aceptan para a Xunta"]
    assert dm.language_ok(ca) and dm.language_ok(gl)
    assert not dm.language_ok(["Breaking news from London about the new law that will be passed by the government"])


def test_find_feed_links():
    html = """<html><head>
      <link rel="alternate" type="application/rss+xml" title="Comentarios" href="/comments/feed">
      <link rel="alternate" type="application/atom+xml" title="Portada" href="/atom.xml">
      <link rel="alternate" type="application/rss+xml" href="https://feeds.diariodemo.es/portada">
      <link rel="alternate" hreflang="en" href="/en/">
      <link rel="stylesheet" href="/s.css">
      <link rel="alternate" type="application/rss+xml" href="javascript:alert(1)">
    </head></html>"""
    assert dm.find_feed_links(html, "https://www.diariodemo.es/") == [
        "https://www.diariodemo.es/atom.xml", "https://feeds.diariodemo.es/portada",
        "https://www.diariodemo.es/comments/feed"]
    assert dm.find_feed_links("<<<not html", "https://x.es/") == []


def test_candidate_feed_urls_limit_and_common_paths():
    urls = dm.candidate_feed_urls("<html></html>", "https://www.diariodemo.es/inicio")
    assert urls == ["https://www.diariodemo.es/feed", "https://www.diariodemo.es/rss"]
    announced = dm.candidate_feed_urls('<link rel="alternate" type="application/rss+xml" href="/x.xml">',
                                       "https://www.diariodemo.es/")
    assert announced[0] == "https://www.diariodemo.es/x.xml" and len(announced) == 2


def test_public_host_guard():
    for bad in ("localhost", "127.0.0.1", "192.168.1.5", "intranet", "printer.local", "", "::1"):
        assert not dm.is_public_host(bad)
    assert dm.is_public_host("www.elpais.com")


def test_registered_domain():
    assert dm.registered_domain("www.elcorreo.com") == "elcorreo.com"
    assert dm.registered_domain("feeds.elpais.com") == "elpais.com"
    assert dm.registered_domain("x.com.es") == "x.com.es"


# --- HTTP flow with a fake web ---------------------------------------------------------------------
def fake_web(routes, log):
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(str(request.url))
        hit = routes.get(str(request.url))
        if hit is None:
            return httpx.Response(404)
        return httpx.Response(200, content=hit)
    return httpx.MockTransport(handler)


def run_site(routes, websites=("https://www.diariodemo.es/",)):
    log: list[str] = []

    async def go():
        async with httpx.AsyncClient(transport=fake_web(routes, log), follow_redirects=True) as client:
            c = {"qid": "Q1", "websites": list(websites)}
            return await dm.check_site(client, dm.Robots(client), c, NOW)
    return asyncio.run(go()), log


HOME = b'<html><head><link rel="alternate" type="application/rss+xml" href="/portada.xml"></head></html>'


def test_check_site_finds_announced_feed_and_respects_request_budget():
    routes = {"https://www.diariodemo.es/": HOME, "https://www.diariodemo.es/portada.xml": rss(),
              "https://www.diariodemo.es/robots.txt": b"User-agent: *\nDisallow: /admin\n"}
    result, log = run_site(routes)
    assert result["reasons"] == [] and result["feed"] == "https://www.diariodemo.es/portada.xml"
    assert result["requests"] == 2
    assert len([u for u in log if not u.endswith("robots.txt")]) == 2


def test_check_site_gives_up_after_three_requests_and_reports_the_last_cause():
    routes = {"https://www.diariodemo.es/": b"<html></html>"}  # no announced feed, common paths answer 404
    result, log = run_site(routes)
    assert result["reasons"] == ["feed_unreachable"]
    content = [u for u in log if not u.endswith("robots.txt")]
    assert len(content) == dm.MAX_REQUESTS_PER_SITE == 3 and result["requests"] == 3


def test_check_site_without_any_feed_path_allowed_says_no_feed():
    routes = {"https://www.diariodemo.es/": b"<html></html>",
              "https://www.diariodemo.es/robots.txt": b"User-agent: *\nDisallow: /feed\nDisallow: /rss\n"}
    result, log = run_site(routes)
    assert result["reasons"] == ["no_feed_found"]
    assert not any(u.endswith(("/feed", "/rss", "/rss.xml")) for u in log)


def test_robots_disallow_means_no_request_to_the_site():
    routes = {"https://www.diariodemo.es/robots.txt": b"User-agent: *\nDisallow: /\n",
              "https://www.diariodemo.es/": HOME}
    result, log = run_site(routes)
    assert result["reasons"] == ["robots_disallowed"]
    assert log == ["https://www.diariodemo.es/robots.txt"]


def test_robots_rules():
    assert dm.Robots.parse(404, b"") == "allow"
    assert dm.Robots.parse(403, b"") == "deny"
    assert dm.Robots.parse(503, b"") == "unknown" and dm.Robots.parse(None, b"") == "unknown"
    rp = dm.Robots.parse(200, b"User-agent: NewsBiasAnalyzer\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert not rp.can_fetch(dm.ROBOTS_TOKEN, "https://x.es/")


def test_unreachable_homepage_and_private_host():
    result, log = run_site({"https://www.diariodemo.es/robots.txt": b""}, ("https://www.diariodemo.es/",))
    assert result["reasons"] == ["homepage_unreachable"]
    result, log = run_site({}, ("http://127.0.0.1/",))
    assert result["reasons"] == ["no_homepage"] and log == []


def test_feed_must_be_https():
    routes = {"https://www.diariodemo.es/": b"<html></html>", "https://www.diariodemo.es/feed": rss()}
    result, _ = run_site(routes)
    assert result["feed"].startswith("https://") and result["reasons"] == []
    http_site = {"http://www.diariodemo.es/": b"<html></html>", "http://www.diariodemo.es/feed": rss()}
    result, _ = run_site(http_site, ("http://www.diariodemo.es/",))
    assert "feed_not_https" in result["reasons"]


# --- candidates from Wikidata ----------------------------------------------------------------------------
def cand(**kw):
    base = {"qid": "Q99", "name": "Diario Demo", "websites": ["https://www.diariodemo.es/"], "domain": "diariodemo.es",
            "inception": 1990, "es_wikipedia": None, "query_types": ["Q11032"]}
    return {**base, **kw}


def test_parse_bindings_merges_websites_and_labels():
    rows = [
        {"item": {"value": "http://www.wikidata.org/entity/Q5"}, "itemLabel": {"value": "El Demo"},
         "site": {"value": "https://eldemo.es/"}, "inception": {"value": "1999-01-01T00:00:00Z"}},
        {"item": {"value": "http://www.wikidata.org/entity/Q5"}, "itemLabel": {"value": "El Demo"},
         "site": {"value": "https://eldemo.es/es/"}, "es": {"value": "https://es.wikipedia.org/wiki/El_Demo"}},
        {"item": {"value": "http://www.wikidata.org/entity/Q6"}, "itemLabel": {"value": "Q6"},
         "site": {"value": "https://sin-etiqueta.es/"}},
    ]
    out = dm.parse_bindings(rows, "Q11032")
    assert out["Q5"]["websites"] == ["https://eldemo.es/", "https://eldemo.es/es/"]
    assert out["Q5"]["inception"] == 1999 and out["Q5"]["es_wikipedia"].endswith("El_Demo")
    assert out["Q6"]["name"] is None  # a bare QID is not a name


def test_query_asks_for_spain_website_and_not_dissolved():
    q = dm.build_query("Q11032")
    for needle in ("wd:Q11032", "wdt:P31/wdt:P279*", "wd:Q29", "wdt:P856", "wdt:P576", "wdt:P291", "wdt:P571"):
        assert needle in q
    assert "wdt:P856" not in dm.build_query("Q19046104", details=False)


def test_exclusions():
    domains = {"elpais.com", "cincodias.elpais.com"}

    def ex(c, qids=(), satire=(), topics=()):
        return dm.exclusion_reason(c, domains, set(qids), set(satire), set(topics))

    assert ex(cand()) is None
    assert ex(cand(qid="Q50009"), qids={"Q50009"}) == "already_in_catalog"
    assert ex(cand(websites=["https://elpais.com/"], domain="elpais.com")) == "already_in_catalog"
    assert ex(cand(websites=["https://motor.elpais.com/"], domain="motor.elpais.com")) == "already_in_catalog"
    assert ex(cand(websites=["https://www.elpais.com.evil.example/"], domain="elpais.com.evil.example")) is None
    assert ex(cand(), satire={"Q99"}) == "satire"
    assert ex(cand(name="El Jueves Sátira Diaria")) == "satire"
    assert ex(cand(), topics={"Q99"}) == "topic_excluded"
    assert ex(cand(name="Diario Deportivo del Sur")) == "topic_excluded"
    assert ex(cand(name="Expansión Economía y Bolsa")) is None  # economy stays
    assert ex(cand(websites=["http://localhost/"], domain="localhost")) == "no_homepage"


def real_catalog_data():
    return (json.loads((ROOT / "data" / "groups.json").read_text(encoding="utf-8"))["groups"],
            json.loads((ROOT / "data" / "media.json").read_text(encoding="utf-8"))["media"])


def test_owner_group_matching_uses_registered_groups_only():
    groups, media = real_catalog_data()
    lookup = dm.group_lookup(groups, media)
    assert dm.owner_group([("Q1337706", "Vocento")], lookup) == "vocento"
    assert dm.owner_group([("Q000", "Grupo Prisa")], lookup) == "prisa"
    assert dm.owner_group([("Q000", "Una Sociedad Limitada Cualquiera")], lookup) is None
    # a co-owner of one outlet (Liberty Acquisition Holdings owns part of El País) does not become the group
    assert dm.owner_group([("Q95735556", "Liberty Acquisition Holdings")], lookup) is None


def test_build_entry_shape():
    facts = {"qid": "Q99", "owners": ["Vocento"], "owners_qids": ["Q1337706"], "kinds_qids": ["Q1110794"],
             "founded": 1991, "source": "https://www.wikidata.org/wiki/Q99", "retrieved": "2026-11-20"}
    groups, media = real_catalog_data()
    c = cand(feed="https://www.diariodemo.es/rss")
    e = dm.build_entry(c, facts, "Galicia", dm.group_lookup(groups, media), {"otro"})
    assert list(e)[:9] == ["id", "name", "domains", "type", "scope", "region", "founded", "group", "owner_source"]
    assert (e["id"], e["type"], e["scope"], e["region"], e["group"]) == ("diario-demo", "Diario", "Regional",
                                                                       "Galicia", "vocento")
    assert e["evidence"] == [] and e["feeds"] == ["https://www.diariodemo.es/rss"] and e["founded"] == 1991
    assert e["owner_source"] == "https://www.wikidata.org/wiki/Q99" and "publisher" not in e
    taken = dm.build_entry(c, facts, None, {}, {"diario-demo"})
    assert taken["id"] == "diario-demo-q99" and taken["scope"] == "Nacional" and "region" not in taken
    assert taken["group"] is None and taken["publisher"] == "Vocento"
    plain = dm.build_entry(cand(feed="https://x.es/rss"), {"qid": "Q99"}, None, {}, set())
    assert plain["group"] is None and "owner_source" not in plain and "publisher" not in plain


def test_guess_type():
    assert dm.guess_type(["Q1110794"], ["Q11032"]) == "Diario"
    assert dm.guess_type(["Q1153191"], ["Q11032"]) == "Digital"
    assert dm.guess_type([], ["Q14350"]) == "Radio"
    assert dm.guess_type(["Q2001305"], []) == "Televisión"
    assert dm.guess_type([], []) == "Digital"


# --- PR body, stability, --apply ---------------------------------------------------------------------------
ENTRY = {"id": "diario-demo", "name": "Diario Demo", "domains": ["diariodemo.es"], "type": "Diario",
         "scope": "Nacional", "founded": 1990, "group": None, "owner_source": "https://www.wikidata.org/wiki/Q99",
         "feeds": ["https://www.diariodemo.es/rss"], "evidence": [],
         "facts": {"owners": ["Editorial Demo | S.L."], "source": "https://www.wikidata.org/wiki/Q99",
                   "retrieved": "2026-11-20"}}


def test_pr_body_lists_candidates_and_checklist():
    other = {**ENTRY, "id": "radio-x", "name": "Radio X", "domains": ["radiox.es"], "owner_source": None,
             "facts": {}, "feeds": ["https://radiox.es/feed"]}
    report = {"candidates": [{"id": "radio-x", "status": "passed",
                              "identity_source": "https://es.wikipedia.org/wiki/Radio_X"}]}
    body = dm.render_pr_body({"media": [ENTRY, other]}, report)
    assert "| Diario Demo | diariodemo.es | https://www.diariodemo.es/rss | Editorial Demo \\| S.L. |" in body
    assert "[enlace](https://www.wikidata.org/wiki/Q99)" in body
    assert "sin propietario en Wikidata" in body and "https://es.wikipedia.org/wiki/Radio_X" in body
    assert "- [ ]" in body and "--apply" in body and "**2**" in body
    empty = dm.render_pr_body({"media": []})
    assert "**0**" in empty and "| Medio |" not in empty


def test_stabilise_keeps_old_retrieval_date_when_nothing_else_changed():
    old = {**ENTRY, "facts": {**ENTRY["facts"], "retrieved": "2026-11-13"}}
    new = {**ENTRY, "facts": {**ENTRY["facts"], "retrieved": "2026-11-20"}}
    assert dm.stabilise([old], [new])[0]["facts"]["retrieved"] == "2026-11-13"
    changed = {**new, "feeds": ["https://www.diariodemo.es/otro"]}
    assert dm.stabilise([old], [changed])[0]["facts"]["retrieved"] == "2026-11-20"


@pytest.fixture
def data_copy(tmp_path):
    for name in ("media.json", "groups.json", "sponsored_rules.json", "synonyms.json"):
        shutil.copy(ROOT / "data" / name, tmp_path / name)
    return tmp_path


def test_dumps_matches_the_hand_maintained_layout():
    raw = (ROOT / "data" / "media.json").read_bytes()
    assert dm.dumps(json.loads(raw)) == raw


def write_entries(path, *entries):
    path.write_bytes(dm.dumps({"_meta": {}, "media": list(entries)}))


def test_apply_merges_after_the_last_news_outlet_and_keeps_catalog_valid(data_copy):
    entries = data_copy / "media.candidates.json"
    b = {**ENTRY, "id": "b-demo", "domains": ["bdemo.es"]}
    write_entries(entries, ENTRY, b)
    added = dm.apply_candidates(["diario-demo", "b-demo"], data_copy / "media.json", entries,
                                data_copy / "groups.json")
    assert added == ["b-demo", "diario-demo"]
    media = json.loads((data_copy / "media.json").read_text(encoding="utf-8"))["media"]
    ids = [m["id"] for m in media]
    original = json.loads((ROOT / "data" / "media.json").read_text(encoding="utf-8"))["media"]
    last_news = max(i for i, m in enumerate(original) if not m.get("factchecker"))
    assert ids[last_news + 1:last_news + 3] == ["b-demo", "diario-demo"]
    assert [m for m in media if m["id"] not in {"b-demo", "diario-demo"}] == original
    cat = Catalog(data_copy)
    assert "diario-demo" in cat.media and cat.media["diario-demo"].assessment.position is None
    assert (data_copy / "media.json").read_bytes() == dm.dumps(json.loads((data_copy / "media.json").read_bytes()))


def test_apply_refuses_bad_requests_and_changes_nothing(data_copy):
    entries = data_copy / "media.candidates.json"
    clash = {**ENTRY, "id": "otro", "domains": ["elpais.com"]}
    badgroup = {**ENTRY, "id": "g", "domains": ["g.es"], "group": "no-existe"}
    write_entries(entries, ENTRY, clash, badgroup)
    before = (data_copy / "media.json").read_bytes()
    for ids, text in ((["nope"], "no está"), (["otro"], "dominio"), (["g"], "grupo"),
                      (["diario-demo", "nope"], "no está")):
        with pytest.raises(SystemExit) as exc:
            dm.apply_candidates(ids, data_copy / "media.json", entries, data_copy / "groups.json")
        assert text in str(exc.value)
        assert (data_copy / "media.json").read_bytes() == before
    dm.apply_candidates(["diario-demo"], data_copy / "media.json", entries, data_copy / "groups.json")
    with pytest.raises(SystemExit, match="ya existe"):
        dm.apply_candidates(["diario-demo"], data_copy / "media.json", entries, data_copy / "groups.json")


def test_committed_candidate_files_are_consistent():
    """If the repository carries candidates, they must be mergeable and not already in the catalog."""
    entries_file, report_file = ROOT / "data" / "media.candidates.json", ROOT / "data" / "candidates.json"
    if not entries_file.exists():
        pytest.skip("no candidates generated yet")
    groups, media = real_catalog_data()
    taken = {d for m in media for d in m["domains"]}
    ids = {m["id"] for m in media}
    gids = {g["id"] for g in groups}
    for e in json.loads(entries_file.read_text(encoding="utf-8"))["media"]:
        assert e["id"] not in ids and not set(e["domains"]) & taken
        assert e["evidence"] == [] and all(f.startswith("https://") for f in e["feeds"])
        assert e["group"] is None or e["group"] in gids
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["_meta"]["version"] == dm.REPORT_VERSION
    for c in report["candidates"]:
        assert c["status"] in {"passed", "failed"}
        assert (c["status"] == "passed") == (not c["reasons"])


def test_weekly_workflow_is_wired_as_documented():
    text = (ROOT / ".github" / "workflows" / "discover-media.yml").read_text(encoding="utf-8")
    assert 'cron: "23 5 * * 1"' in text and "workflow_dispatch" in text
    assert "contents: write" in text and "pull-requests: write" in text
    assert "bot/media-candidates" in text and "scripts/discover_media.py --pr-body" in text
    # only first-party actions (no third-party action to pin)
    uses = [line.split("uses:")[1].strip() for line in text.splitlines() if "uses:" in line]
    assert uses and all(u.startswith("actions/") for u in uses)


def test_regions_follow_the_administrative_chain(monkeypatch):
    def claim(prop, qid):
        return {prop: [{"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": qid}}}, "rank": "normal"}]}

    graph = {
        "Q1": {"claims": claim("P291", "Q10")},  # outlet published in a town
        "Q2": {"claims": claim("P159", "Q20")},  # outlet headquartered in Madrid city
        "Q3": {"claims": {}},  # no place at all
        "Q10": {"claims": claim("P131", "Q11"), "labels": {"es": {"value": "Vigo"}}},
        "Q11": {"claims": claim("P131", "Q12"), "labels": {"es": {"value": "provincia de Pontevedra"}}},
        "Q12": {"claims": {}, "labels": {"es": {"value": "Galicia"}}},
        "Q20": {"claims": claim("P131", "Q21"), "labels": {"es": {"value": "Madrid"}}},
        "Q21": {"claims": {}, "labels": {"es": {"value": "Comunidad de Madrid"}}},
    }
    monkeypatch.setattr(dm.enrich_media, "fetch_entities",
                        lambda ids, props: {i: graph[i] for i in ids if i in graph})
    assert dm.regions_for({q: graph[q] for q in ("Q1", "Q2", "Q3")}) == {"Q1": "Galicia", "Q2": None}
