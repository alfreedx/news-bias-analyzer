"""ClaimReview ingestion, URL-only story linking, /verificaciones and the 403 / robots / retention rules.

No network: the HTTP layer is an httpx MockTransport. Contraste quotes the checkers and never rates anything.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.claims import fetch_claims, normalize_url, on_own_domain, parse_claim_reviews
from app.db import Database
from app.stories import assign_stories


def page(*blocks, extra=""):
    scripts = "".join(f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>'
                      for b in blocks)
    return f"<html><head><title>x</title>{scripts}</head><body>{extra}</body></html>"


REVIEW = {
    "@context": "https://schema.org", "@type": "ClaimReview", "url": "https://maldita.es/desinfo/1",
    "datePublished": "2026-11-01T10:00:00+01:00", "claimReviewed": "  El Gobierno prohibirá\n las gasolineras  ",
    "reviewRating": {"@type": "Rating", "alternateName": "Falso."},
    "itemReviewed": {"@type": "Claim", "author": {"@type": "Person", "name": "Usuario de X"},
                     "appearance": [{"@type": "CreativeWork", "url": "https://www.Abc.es/noticia-1/?utm_source=x#top"},
                                    "https://elpais.com/n2"],
                     "firstAppearance": {"url": "https://x.com/post/1"}},
}


# --- parser ----------------------------------------------------------------------------
def test_parses_single_review_verbatim():
    [r] = parse_claim_reviews(page(REVIEW))
    assert r.claim == "El Gobierno prohibirá las gasolineras"  # whitespace collapsed, nothing else changed
    assert r.rating == "Falso."  # verbatim, trailing period included
    assert r.claimant == "Usuario de X"
    assert r.reviewed_urls == ["https://www.Abc.es/noticia-1/?utm_source=x#top", "https://elpais.com/n2",
                               "https://x.com/post/1"]
    assert r.published and r.published < time.time()


def test_parses_list_graph_nested_and_several_reviews():
    other = {**REVIEW, "claimReviewed": "Otra afirmación", "reviewRating": {"alternateName": "Engañoso"}}
    assert [r.claim for r in parse_claim_reviews(page([REVIEW, {"@type": "WebSite"}, other]))] == [
        "El Gobierno prohibirá las gasolineras", "Otra afirmación"]
    graph = {"@context": "https://schema.org", "@graph": [{"@type": "Organization"}, REVIEW]}
    assert len(parse_claim_reviews(page(graph))) == 1
    nested = {"@type": "WebPage", "mainEntity": {"@type": ["Article", "ClaimReview"], **{
        k: v for k, v in REVIEW.items() if k != "@type"}}}
    assert len(parse_claim_reviews(page(nested))) == 1
    assert len(parse_claim_reviews(page(REVIEW, other))) == 2  # two blocks in one page
    assert len(parse_claim_reviews(page(REVIEW, REVIEW))) == 1  # exact repeat ignored


def test_malformed_missing_and_hostile_data():
    assert parse_claim_reviews(page("{not json")) == []
    assert parse_claim_reviews(page('{"@type": "ClaimReview"}')) == []  # no claim
    assert parse_claim_reviews("<html>sin datos</html>") == []
    assert parse_claim_reviews(b"\xff\xfe<script type='application/ld+json'>[1,2]</script>") == []
    [r] = parse_claim_reviews(page({"@type": "ClaimReview", "claimReviewed": "Solo la afirmación"}))
    assert r.rating is None and r.claimant is None and r.reviewed_urls == []
    [r] = parse_claim_reviews(page({"@type": "ClaimReview", "claimReviewed": "x" * 900,
                                    "itemReviewed": {"appearance": ["javascript:alert(1)", "ftp://x/y", 5, None]}}))
    assert len(r.claim) <= 300 and r.reviewed_urls == []
    deep = "[" * 5000 + "]" * 5000  # recursion bomb: ignored, never raises
    assert parse_claim_reviews(page(deep)) == []


def test_review_text_is_plain_text_not_markup():
    [r] = parse_claim_reviews(page({"@type": "ClaimReview", "claimReviewed": "<b>Hola</b> &amp; adiós"}))
    assert r.claim == "Hola & adiós"


# --- URL normalisation -------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [
    ("http://WWW.Example.com/a/b/", "https://example.com/a/b"),
    ("https://example.com/a?utm_source=tw&utm_medium=x", "https://example.com/a"),
    ("https://example.com/a?id=2&fbclid=abc#frag", "https://example.com/a?id=2"),
    ("https://example.com/a?b=1&a=2", "https://example.com/a?a=2&b=1"),
])
def test_normalize_url_equivalents(a, b):
    assert normalize_url(a) == normalize_url(b) == normalize_url(b)


def test_normalize_url_keeps_different_things_apart():
    assert normalize_url("https://example.com/a") != normalize_url("https://example.com/b")
    assert normalize_url("https://example.com/a?id=1") != normalize_url("https://example.com/a?id=2")
    assert normalize_url("https://sub.example.com/a") != normalize_url("https://example.com/a")
    assert normalize_url("javascript:alert(1)") is None and normalize_url("") is None and normalize_url(None) is None


def test_own_domain_rule():
    assert on_own_domain("https://verifica.efe.com/x", ["verifica.efe.com"])
    assert on_own_domain("https://www.maldita.es/x", ["maldita.es"])
    assert not on_own_domain("https://maldita.es.evil.test/x", ["maldita.es"])
    assert not on_own_domain("https://evil.test/maldita.es", ["maldita.es"])


# --- fetching ---------------------------------------------------------------------------
def checkers(*pairs):
    media = [SimpleNamespace(id=i, name=i.title(), domains=[d]) for i, d in pairs]
    return SimpleNamespace(factcheckers=media, media={m.id: m for m in media})


def seed(db, mid, urls):
    now = int(time.time())
    db.insert_articles([(u, mid, f"Titular {i} de {mid}", "", now - i) for i, u in enumerate(urls)])


def run(cat, db, handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return asyncio.run(fetch_claims(cat, db, client=client, **kw))


def test_client_identifies_itself_like_the_ingest():
    from app.claims import make_client
    from app.config import settings
    assert make_client().headers["user-agent"] == settings.user_agent
    assert make_client().follow_redirects is False


def test_fetch_stores_reviews_and_links_by_url_only(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = checkers(("maldita", "maldita.es"))
    seed(db, "maldita", ["https://maldita.es/desinfo/1", "https://maldita.es/desinfo/2"])
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /privado/\n")
        return httpx.Response(200, text=page(REVIEW) if request.url.path.endswith("1") else page(), headers={})

    assert run(cat, db, handler) == 1
    assert sorted(set(seen)) == ["https://maldita.es/desinfo/1", "https://maldita.es/desinfo/2",
                                 "https://maldita.es/robots.txt"]
    hit = db.claim_reviews_for_urls(["https://abc.es/noticia-1"])  # normalised: www., case, utm, fragment
    assert [h["claim"] for h in hit] == ["El Gobierno prohibirá las gasolineras"]
    assert hit[0]["rating"] == "Falso."
    assert db.claim_reviews_for_urls(["https://abc.es/noticia-2", "https://maldita.es/desinfo/1"]) == []
    seen.clear()
    assert run(cat, db, handler) == 0 and seen == []  # pages already read are not fetched again


def test_fetch_limit_per_cycle_and_foreign_domain(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = checkers(("maldita", "maldita.es"))
    seed(db, "maldita", ["https://evil.test/p"] + [f"https://maldita.es/p{i}" for i in range(7)])
    hits = []

    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text=page(REVIEW))

    run(cat, db, handler, limit=3)
    pages = [h for h in hits]
    assert "evil.test" not in pages and len(pages) == 3 + 1  # three pages and one robots.txt
    with db.connect() as c:
        assert c.execute("SELECT status FROM claim_pages WHERE url = 'https://evil.test/p'").fetchone()[0] == "skipped"


def test_403_is_respected_no_retry_and_host_rests(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = checkers(("newtral", "newtral.es"), ("maldita", "maldita.es"))
    seed(db, "newtral", [f"https://www.newtral.es/a{i}" for i in range(5)])
    seed(db, "maldita", ["https://maldita.es/desinfo/1"])
    calls = {"newtral": 0, "maldita": 0}

    def handler(request):
        key = "newtral" if "newtral" in request.url.host else "maldita"
        calls[key] += 1
        if key == "newtral":
            return httpx.Response(403, text="forbidden")
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text=page(REVIEW))

    run(cat, db, handler)
    assert calls["newtral"] == 1  # robots.txt refused: nothing else was requested, nothing retried
    assert calls["maldita"] == 2 and "www.newtral.es" in db.hosts_resting() or "newtral.es" in db.hosts_resting()
    run(cat, db, handler)  # next cycle: the resting host is not contacted at all
    assert calls["newtral"] == 1
    # after the pause the host is tried again
    with db.connect() as c:
        c.execute("UPDATE host_backoff SET until = ?", (int(time.time()) - 1,))
    run(cat, db, handler)
    assert calls["newtral"] == 2


def test_403_on_the_page_itself_also_rests_the_host(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = checkers(("verificat", "verificat.cat"))
    seed(db, "verificat", [f"https://www.verificat.cat/a{i}" for i in range(4)])
    pages = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        pages.append(request.url.path)
        return httpx.Response(403)

    run(cat, db, handler)
    assert len(pages) <= 2  # at most the per-host concurrency; the rest never goes out
    assert "verificat.cat" in db.hosts_resting()
    before = len(pages)
    run(cat, db, handler)
    assert len(pages) == before


def test_robots_disallow_is_respected(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = checkers(("maldita", "maldita.es"))
    seed(db, "maldita", ["https://maldita.es/desinfo/1"])
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /desinfo/\n")

    assert run(cat, db, handler) == 0
    assert requested == ["/robots.txt"]
    assert db.claim_pages_pending(["maldita"], since=0, retry_before=2**40, limit=5) == []  # not asked again


def test_network_errors_never_raise_and_are_retried_later(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = checkers(("maldita", "maldita.es"))
    seed(db, "maldita", ["https://maldita.es/desinfo/1"])

    def boom(request):
        raise httpx.ConnectError("down")

    assert run(cat, db, boom) == 0  # robots.txt unreachable: nothing fetched, nothing marked
    assert len(db.claim_pages_pending(["maldita"], since=0, retry_before=0, limit=5)) == 1

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ReadTimeout("slow")

    assert run(cat, db, handler) == 0
    assert db.claim_pages_pending(["maldita"], since=0, retry_before=0, limit=5) == []  # 'error' waits 6 h
    assert len(db.claim_pages_pending(["maldita"], since=0, retry_before=2**40, limit=5)) == 1


def test_oversized_page_is_dropped(tmp_path, monkeypatch):
    from app import claims
    monkeypatch.setattr(claims, "MAX_PAGE_BYTES", 100)
    db = Database(tmp_path / "n.sqlite3")
    cat = checkers(("maldita", "maldita.es"))
    seed(db, "maldita", ["https://maldita.es/desinfo/1"])

    def handler(request):
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text=page(REVIEW))

    assert run(cat, db, handler) == 0


def test_retention_purge_and_idempotent_migration(tmp_path):
    path = tmp_path / "n.sqlite3"
    db = Database(path)
    Database(path)  # second start: migration is idempotent
    now = int(time.time())
    old = parse_claim_reviews(page({**REVIEW, "datePublished": "2020-01-01T00:00:00Z"}))
    new = parse_claim_reviews(page({**REVIEW, "claimReviewed": "Reciente"}))
    db.save_claim_reviews("https://maldita.es/old", "maldita", "t", now, old)
    db.save_claim_reviews("https://maldita.es/new", "maldita", "t", now, new)
    assert db.purge_claims(730) == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM claim_reviews").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM claim_review_urls").fetchone()[0] == 3  # only the kept review's
        assert c.execute("SELECT COUNT(*) FROM claim_reviews_fts WHERE claim_reviews_fts MATCH 'gasolineras'"
                         ).fetchone()[0] == 0
    # reviews outlive the articles: the 120-day article purge leaves them alone
    db.insert_articles([("https://maldita.es/x", "maldita", "viejo", "", now - 200 * 86400)])
    db.purge_older_than(120)
    assert db.verifications("reciente", media_ids=["maldita"])[0]["claim"] == "Reciente"


# --- pages and API ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    now = int(time.time())
    main.db.insert_articles([
        ("https://eldiario.es/z1", "eldiario-es", "Zumbalino anuncia el cierre de las fronteras interiores", "Cifra 77.", now),
        ("https://abc.es/z2", "abc", "Zumbalino anuncia el cierre de las fronteras interiores hoy", "Cifra 77.", now),
        ("https://publico.es/z3", "publico", "El cierre de las fronteras interiores que anuncia Zumbalino", "77", now),
        ("https://okdiario.es/z4", "okdiario", "Zumbalino anuncia cierre fronteras interiores esta tarde", "77", now),
        # same words, but no link: must never show on the story page
        ("https://maldita.es/desinfo/zumbalino", "maldita", "Falso: Zumbalino no cierra las fronteras interiores", "", now),
        ("https://maldita.es/desinfo/linked", "maldita", "Titular enlazado Zumbalino fronteras", "", now),
        ("https://www.newtral.es/zumbalino-bulo", "newtral", "Newtral comprueba el mensaje sobre Zumbalino", "", now),
    ])
    assign_stories(main.db, main.catalog)
    main.db.save_claim_reviews("https://maldita.es/desinfo/linked", "maldita", "Titular enlazado Zumbalino fronteras", now, [
        parse_claim_reviews(page({
            **REVIEW, "claimReviewed": "Circula que la frontera cierra el lunes", "datePublished": None,
            "itemReviewed": {"author": {"name": "Cadena de WhatsApp"}, "appearance": ["https://WWW.abc.es/z2/?utm_x=1"]},
        }))[0]])
    with TestClient(main.app) as c:
        yield c


def story_html(client, url):
    with main.db.connect() as c:
        sid = c.execute("SELECT story_id FROM story_articles sa JOIN articles a ON a.id = sa.article_id "
                        "WHERE a.url = ?", (url,)).fetchone()[0]
    r = client.get(f"/historia/{sid}")
    assert r.status_code == 200
    return r.text


def test_story_shows_review_only_through_explicit_url(client):
    html = story_html(client, "https://abc.es/z2")
    assert "Lo que se dice: «Circula que la frontera cierra el lunes»" in html
    assert "Maldita.es lo califica: <strong>Falso.</strong>" in html
    assert "Atribuido a: Cadena de WhatsApp" in html
    assert "Leer la verificación completa" in html and 'rel="noopener noreferrer"' in html
    assert "No hemos encontrado verificaciones enlazadas" not in html


def test_story_keyword_overlap_is_not_a_link(client):
    """Regression: a checker headline sharing every keyword with a story, but with no URL link, shows nothing."""
    main.db.save_claim_reviews("https://maldita.es/desinfo/linked", "maldita", "Titular enlazado Zumbalino fronteras", int(time.time()), [])
    try:
        html = story_html(client, "https://abc.es/z2")
        assert "Falso: Zumbalino no cierra" not in html and "Newtral comprueba" not in html
        assert "lo califica" not in html and "Lo que se dice" not in html
        assert ("No hemos encontrado verificaciones enlazadas a estas noticias. "
                "Eso no significa que sean ciertas ni falsas.") in html
        assert 'href="/verificaciones?q=' in html and "Ver verificaciones sobre este tema" in html
        assert "ningún verificador" not in html.lower()
    finally:
        parsed = parse_claim_reviews(page({**REVIEW, "claimReviewed": "Circula que la frontera cierra el lunes",
                                           "itemReviewed": {"author": {"name": "Cadena de WhatsApp"},
                                                            "appearance": ["https://WWW.abc.es/z2/?utm_x=1"]}}))
        main.db.save_claim_reviews("https://maldita.es/desinfo/linked", "maldita", "Titular enlazado Zumbalino fronteras",
                                   int(time.time()), parsed)


def test_search_has_no_checks_in_briefs_and_labels_term_matches(client):
    html = client.get("/buscar", params={"q": "Zumbalino fronteras"}).text
    assert "Verificaciones que mencionan estos términos" in html
    assert "no necesariamente tratan de estas noticias" in html.lower() or "No necesariamente tratan" in html
    assert "Verificaciones:" not in html and "Ningún verificador" not in html
    assert "Falso: Zumbalino no cierra las fronteras interiores" in html  # headline only: no rating deduced
    data = client.get("/api/buscar", params={"q": "Zumbalino fronteras"}).json()
    assert data["factchecks"]
    assert all(c["rating"] is None for c in data["factchecks"] if c["claim"] is None)  # never from the headline
    assert any(c["rating"] == "Falso." for c in data["factchecks"])
    shown = client.get("/buscar", params={"q": "Zumbalino fronteras"}).text
    assert "Maldita.es lo califica: <strong>Falso.</strong>" in shown  # rating only from the ClaimReview


def test_verificaciones_page_and_api(client):
    r = client.get("/verificaciones")
    assert r.status_code == 200 and "set-cookie" not in r.headers
    html = r.text
    assert "¿Es verdad lo que has visto?" in html and 'name="q"' in html
    assert "Lo que se dice: «Circula que la frontera cierra el lunes»" in html
    assert "Newtral comprueba el mensaje sobre Zumbalino" in html  # headline + link, no rating
    assert "Contraste no califica nada" in html and "no significa que algo sea cierto" in html
    assert "style=" not in html and "<script>" not in html
    q = client.get("/verificaciones", params={"q": "frontera lunes"}).text
    assert "Circula que la frontera" in q and "Newtral comprueba" not in q
    empty = client.get("/verificaciones", params={"q": "palabrainexistentexyz"}).text
    assert "No hemos encontrado verificaciones con «palabrainexistentexyz»" in empty
    assert "no significa que algo sea cierto ni falso" in empty
    xss = client.get("/verificaciones", params={"q": "<script>alert(1)</script>"}).text
    assert "<script>alert" not in xss
    assert client.get("/verificaciones", params={"q": "a"}).status_code == 200

    data = client.get("/api/verificaciones").json()
    assert {"claim", "rating", "claimant", "source", "url", "headline", "published"} <= set(data["verifications"][0])
    by_url = {v["url"]: v for v in data["verifications"]}
    assert by_url["https://maldita.es/desinfo/linked"]["rating"] == "Falso."
    assert by_url["https://www.newtral.es/zumbalino-bulo"]["rating"] is None
    assert [v["url"] for v in client.get("/api/verificaciones", params={"q": "WhatsApp"}).json()["verifications"]] == []
    found = client.get("/api/verificaciones", params={"q": "lunes"}).json()["verifications"]
    assert [v["url"] for v in found] == ["https://maldita.es/desinfo/linked"]
    assert client.get("/api/verificaciones", params={"q": "a"}).status_code == 422
    assert client.get("/api/verificaciones", params={"q": "x" * 600}).status_code == 422


def test_nav_and_home_link_to_verificaciones(client):
    assert 'href="/verificaciones"' in client.get("/").text
    assert 'href="/verificaciones"' in client.get("/privacidad").text
    assert "navegador" in client.get("/privacidad").text and "servidor (no tu navegador)" in client.get("/privacidad").text
