import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.security import RateLimitMiddleware


@pytest.fixture(scope="module")
def client():
    now = int(time.time())
    main.db.insert_articles([
        ("https://elpais.com/v1", "el-pais", "El Gobierno anuncia un plan de vivienda pública", "Resumen", now),
        ("https://www.abc.es/v2", "abc", "El Gobierno anuncia un plan de vivienda que no convence", "", now),
        ("https://maldita.es/v3", "maldita", "Bulo sobre el plan de vivienda", "", now),
        ("https://okdiario.com/x", "okdiario", "<script>alert('x')</script> vivienda", "", now),
    ])
    with TestClient(main.app) as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/medios", "/medios/el-pais", "/medios/20minutos", "/grupos/prisa",
                                  "/metodologia", "/privacidad", "/acerca", "/buscar", "/buscar?q=vivienda",
                                  "/api/medios", "/api/salud", "/robots.txt", "/.well-known/security.txt"])
def test_pages_ok(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "set-cookie" not in r.headers


def test_security_headers(client):
    h = client.get("/").headers
    assert "default-src 'none'" in h["content-security-policy"]
    assert "unsafe-inline" not in h["content-security-policy"]
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"


def test_no_inline_scripts_or_styles(client):
    html = client.get("/buscar?q=vivienda").text
    assert "<script>" not in html  # only <script src=...>
    assert "style=" not in html


def test_search_results_escape_html(client):
    html = client.get("/buscar?q=vivienda").text
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    q = client.get("/buscar", params={"q": '"><img src=x onerror=alert(1)>'}).text
    assert "<img src=x" not in q


def test_search_groups_and_factchecks(client):
    data = client.get("/api/buscar", params={"q": "plan vivienda"}).json()
    assert data["coverage"]["total"] == 2
    assert any(len(s) == 2 for s in data["stories"])
    assert data["factchecks"][0]["source"] == "Maldita.es"


def test_bad_inputs(client):
    assert client.get("/medios/../../etc/passwd").status_code == 404
    assert client.get("/medios/nope").status_code == 404
    assert client.get("/api/buscar", params={"q": "a"}).status_code == 422
    assert client.get("/api/buscar", params={"q": "x" * 600}).status_code == 422
    assert client.get("/buscar", params={"q": "vivienda", "dias": "'; drop table"}).status_code == 200


def test_untrusted_host_is_rejected(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_rate_limit_returns_429():
    mw = RateLimitMiddleware(app=None, per_minute=2)

    class Req:
        class url:
            path = "/buscar"
        client = type("c", (), {"host": "1.2.3.4"})()
        headers = {}

    async def ok(_):
        return "ok"

    import asyncio
    results = [asyncio.run(mw.dispatch(Req, ok)) for _ in range(3)]
    assert results[:2] == ["ok", "ok"]
    assert results[2].status_code == 429
    # the raw IP is never kept
    assert all("1.2.3.4" not in k for k in mw.hits)


def test_home_renders_blind_spots_section(client):
    from app.analysis import Story, blind_spots, make_article
    rights = [m.id for m in main.catalog.news_media if (m.assessment.position or 0) > 0][:3]
    arts = [make_article(main.catalog, title=f"Titular {i} sobre el asunto", url=f"https://x.test/{i}", summary="",
                         published=int(time.time()), media_id=m) for i, m in enumerate(rights)]
    main.state.blind_spots = blind_spots([Story(arts, seed=arts[0])])
    try:
        html = client.get("/").text
        assert "Puntos ciegos" in html and "Cubierta sobre todo por medios de derecha" in html
        assert "style=" not in html
    finally:
        main.state.blind_spots = []
    assert "Puntos ciegos" not in client.get("/").text


def test_author_filter_route(client):
    main.db.insert_articles([("https://elpais.com/au1", "el-pais", "Titular firmado uno", "", int(time.time()), "Ana Pérez"),
                             ("https://abc.es/au2", "abc", "Titular firmado dos", "", int(time.time()), "Ana Pérez")])
    html = client.get("/buscar", params={"autor": "Ana Pérez"}).text
    assert "Titular firmado uno" in html and "Titular firmado dos" in html
    assert "/medios/el-pais" in html and "/medios/abc" in html
    assert "style=" not in html
    assert "No hay titulares" in client.get("/buscar", params={"autor": "Nadie Inexistente"}).text
    xss = client.get("/buscar", params={"autor": "<script>alert(1)</script>"}).text
    assert "<script>alert" not in xss
    result = client.get("/buscar", params={"q": "firmado"}).text
    assert "buscar?autor=Ana%20P%C3%A9rez" in result


def test_story_route_and_links(client):
    from app import main
    from app.stories import assign_stories
    now = int(time.time())
    main.db.insert_articles([
        ("https://eldiario.es/h1", "eldiario-es", "Illa y Puigdemont pactan la financiación singular catalana", "Cifra de 12,5 millones.", now),
        ("https://abc.es/h2", "abc", "Illa y Puigdemont pactan la financiación singular catalana hoy", "Cifra de 12,5 millones.", now),
        ("https://publico.es/h3", "publico", "Puigdemont e Illa pactan la financiación singular catalana", "12,5 millones", now),
        ("https://okd.es/h4", "okdiario", "Pactan Illa y Puigdemont financiación singular catalana", "12,5 millones", now),
    ])
    assert assign_stories(main.db, main.catalog)["created"] >= 1
    with main.db.connect() as c:
        sid = c.execute("SELECT story_id FROM story_articles sa JOIN articles a ON a.id = sa.article_id "
                        "WHERE a.url = 'https://abc.es/h2'").fetchone()[0]
    r = client.get(f"/historia/{sid}")
    assert r.status_code == 200 and "En lo que coinciden" in r.text and "set-cookie" not in r.headers
    for banned in ("probado", "verdad", "hecho comprobado"):
        assert banned not in r.text.lower()
    assert "no prueba que algo sea cierto" in r.text
    assert "style=" not in r.text and "<script>" not in r.text
    assert "12,5 millones" in r.text and "<details" in r.text
    main.build_front_page()
    assert f"/historia/{sid}" in client.get("/buscar?q=financiación+singular").text
    assert client.get("/historia/999999").status_code == 404
    assert client.get("/historia/99999999999999999999999").status_code == 404
    assert client.get("/historia/abc").status_code == 404
    assert "Allow: /historia/" in client.get("/robots.txt").text
    assert "Disallow: /buscar" in client.get("/robots.txt").text
