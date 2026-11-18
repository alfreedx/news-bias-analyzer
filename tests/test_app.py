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
