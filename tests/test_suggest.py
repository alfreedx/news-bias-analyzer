"""Suggestions while typing (/api/sugerencias), their ranking, and «Quizá buscabas…» on /buscar."""

import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

from app import main, suggest
from app.db import Database
from app.security import RateLimitMiddleware

NOW = int(time.time())


def make_db(tmp_path, rows, stories=()):
    """rows: (url, media_id, title[, summary]); stories: (title, [urls]) persisted stories."""
    db = Database(tmp_path / "s.sqlite3")
    db.insert_articles([(r[0], r[1], r[2], r[3] if len(r) > 3 else "", NOW - i * 60) for i, r in enumerate(rows)])
    add_stories(db, stories)
    return db


def add_stories(db, stories, updated=None):
    with db.connect() as c:
        for title, urls in stories:
            sid = c.execute("INSERT INTO stories(created, updated, title, keywords) VALUES (?, ?, ?, '')",
                            (NOW, updated or NOW, title)).lastrowid
            for u in urls:
                aid = c.execute("SELECT id FROM articles WHERE url = ?", (u,)).fetchone()[0]
                c.execute("INSERT INTO story_articles(story_id, article_id) VALUES (?, ?)", (sid, aid))


ROWS = [
    ("u1", "el-pais", "Rescate de astronautas en la base lunar Selene"),
    ("u2", "abc", "La base lunar Selene recibe a los astronautas rescatados"),
    ("u3", "elmundo", "Astronautas varados en la Luna esperan el rescate"),
    ("u4", "eldiario-es", "Las asambleas de vecinos lunares aprueban el nuevo estatuto"),
    ("u5", "okdiario", "Estatuto lunar: los vecinos aprueban las asambleas"),
    ("u6", "la-razon", "Un joven cobra una paga de pádel lunar"),
    ("u7", "publico", "Una subvención de pádel lunar para jóvenes"),
    ("u8", "20minutos", "Titular suelto sobre astronautas veteranos jubilados"),
]
STORIES = [
    ("Rescate de astronautas en la base lunar", ["u1", "u2", "u3"]),
    ("Los vecinos lunares aprueban su estatuto", ["u4", "u5"]),
    ("Paga de pádel lunar", ["u6", "u7"]),
]


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path, ROWS, STORIES)


def titles(items):
    return [i["title"] for i in items]


# --- ranking -----------------------------------------------------------------------------------------------------
def test_last_token_is_a_prefix_and_earlier_tokens_are_whole_words(db):
    stories, _ = suggest.suggest(db, "rescate astron")
    assert titles(stories) == ["Rescate de astronautas en la base lunar"]
    # an earlier word is NOT a prefix: «resc» finds nothing when it is not the last token
    partial = suggest.similar_stories(db, "resc astronautas", prefix_last=True, require_last=True)
    assert [p["matched"] for p in partial] == [1]  # only «astronautas» counted
    assert suggest.similar_stories(db, "rescate astronautas", prefix_last=True)[0]["matched"] == 2
    # the word being typed must match
    assert suggest.similar_stories(db, "rescate zzz", prefix_last=True, require_last=True) == []


def test_stories_are_aggregated_with_outlet_count(db):
    stories, _ = suggest.suggest(db, "astronautas")
    top = stories[0]
    assert top["title"] == "Rescate de astronautas en la base lunar"
    assert top["outlets"] == 3
    assert len([s for s in stories if s["id"] == top["id"]]) == 1  # three matching headlines, one suggestion


def test_synonyms_expand_earlier_words(db):
    stories, _ = suggest.suggest(db, "ayuda pádel lun")  # «ayuda» is equivalent of «paga» / «subvención»
    assert titles(stories)[:1] == ["Paga de pádel lunar"]


def test_ranking_by_matched_tokens_then_outlets(db):
    got = suggest.similar_stories(db, "astronautas lunares estatuto", prefix_last=False)
    assert got[0]["title"] == "Los vecinos lunares aprueban su estatuto"  # 2 of 3 words, ahead of 1-word matches
    assert got[0]["matched"] == 2
    assert all(g["matched"] <= got[0]["matched"] for g in got)
    one = suggest.similar_stories(db, "astronautas lunares", prefix_last=False)
    assert one[0]["matched"] == 2 and one[0]["title"] == "Rescate de astronautas en la base lunar"


def test_fills_with_headlines_when_few_stories(db):
    stories, headlines = suggest.suggest(db, "veteranos jubil")
    assert stories == []
    assert titles(headlines) == ["Titular suelto sobre astronautas veteranos jubilados"]
    assert headlines[0]["media_id"] == "20minutos"
    assert headlines[0]["keywords"] == "titular suelto astronautas veteranos"


def test_headlines_exclude_those_inside_listed_stories(db):
    stories, headlines = suggest.suggest(db, "astronaut")
    assert stories and all("Rescate" not in h["title"] for h in headlines)
    assert titles(headlines) == ["Titular suelto sobre astronautas veteranos jubilados"]


def test_no_headlines_when_three_stories_found(tmp_path):
    rows = [(f"u{i}", "abc", f"Cohete {i} despega rumbo a Marte") for i in range(6)]
    db = make_db(tmp_path, rows, [(f"Cohete {i}", [f"u{2 * i}", f"u{2 * i + 1}"]) for i in range(3)])
    stories, headlines = suggest.suggest(db, "cohete")
    assert len(stories) == 3 and headlines == []


def test_never_more_than_six(tmp_path):
    rows = [(f"u{i}", "abc", f"Cohete número {i} despega") for i in range(24)]
    db = make_db(tmp_path, rows, [(f"Cohete {i}", [f"u{2 * i}", f"u{2 * i + 1}"]) for i in range(12)])
    stories, headlines = suggest.suggest(db, "cohete")
    assert len(stories) == 6 and headlines == []


def test_only_last_30_days_and_no_sponsored(tmp_path):
    db = Database(tmp_path / "s.sqlite3")
    old = NOW - 40 * 86400
    db.insert_articles([("a1", "abc", "Eclipse total sobre Soria", "", old), ("a2", "elmundo", "Eclipse sobre Soria", "", old)])
    add_stories(db, [("Eclipse antiguo", ["a1", "a2"])])
    assert suggest.suggest(db, "eclipse") == ([], [])


def test_min_and_max_length():
    db = None  # never touched for too-short text
    assert suggest.suggest(db, "ab") == ([], [])
    assert suggest.suggest(db, "  a  ") == ([], [])
    assert len(suggest.normalise("x" * 500)) == suggest.MAX_CHARS
    assert suggest.normalise("  hola \x00\x07  mundo\n") == "hola mundo"


def test_long_input_is_truncated_not_rejected(db):
    stories, _ = suggest.suggest(db, "astronautas " + "relleno " * 200)
    assert isinstance(stories, list)  # truncated to 100 chars; the extra words never reach the query


@pytest.mark.parametrize("hostile", ['" OR 1=1 --', "astro* NEAR(a b)", 'title : "x" AND', "(((", "'; DROP TABLE articles; --",
                                     'a"b"c"*', "\\", "{title}: foo", "^astro", "-astro", "AND OR NOT"])
def test_hostile_input_stays_inside_quotes(db, hostile):
    p = suggest.plan(hostile, prefix_last=True)
    if p:
        for expr in p.exprs:
            assert all(c.isalnum() or c in ' "*()_' for c in expr)
            assert expr.count('"') % 2 == 0
    suggest.suggest(db, hostile)  # must not raise
    suggest.similar_stories(db, hostile, prefix_last=False)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == len(ROWS)


def test_cache_is_lru_with_ttl():
    now = [0.0]
    c = suggest.SuggestCache(size=2, ttl=10, clock=lambda: now[0])
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1  # a is now the most recent
    c.put("c", 3)
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
    now[0] = 11
    assert c.get("a") is None and len(c) == 1


# --- the API -------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    now = int(time.time())
    main.db.insert_articles([
        ("https://x.test/s1", "el-pais", "Zarzamora: el Gobierno lanza un plan de riego para olivares", "", now),
        ("https://x.test/s2", "abc", "Zarzamora, plan de riego para los olivares del sur", "", now),
        ("https://x.test/s3", "elmundo", "Plan de riego en olivares: zarzamora y otras medidas", "", now),
        ("https://x.test/s4", "okdiario", "Zarzamora aparte: titular suelto sobre ranas", "", now),
    ])
    add_stories(main.db, [("Plan de riego para olivares", ["https://x.test/s1", "https://x.test/s2", "https://x.test/s3"])])
    with TestClient(main.app) as c:
        yield c


def test_api_shape(client):
    r = client.get("/api/sugerencias", params={"q": "zarzam"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    items = r.json()["suggestions"]
    story = items[0]
    assert set(story) == {"id", "title", "outlets", "url", "when"}
    assert story["title"] == "Plan de riego para olivares"
    assert story["outlets"] == 3 and story["url"] == f"/historia/{story['id']}" and story["when"] == "ahora"
    head = items[1]
    assert set(head) == {"title", "outlet", "url"}
    assert head["outlet"] == "OKDIARIO"
    assert head["url"].startswith("/buscar?q=") and "zarzamora" in head["url"]
    assert len(items) <= 6


def test_api_short_or_missing_q_gives_empty_list(client):
    for params in ({"q": "ab"}, {"q": "  a "}, {}, {"q": ""}):
        r = client.get("/api/sugerencias", params=params)
        assert r.status_code == 200 and r.json() == {"suggestions": []}


def test_api_long_q_is_accepted(client):
    r = client.get("/api/sugerencias", params={"q": "zarzamora " * 200})
    assert r.status_code == 200


def test_api_caches_results(client, monkeypatch):
    main._suggest_cache.clear()
    calls = []
    real = main.suggest
    monkeypatch.setattr(main, "suggest", lambda db, q: calls.append(q) or real(db, q))
    a = client.get("/api/sugerencias", params={"q": "Zarzam"}).json()
    b = client.get("/api/sugerencias", params={"q": "zarzam"}).json()
    assert a == b and len(calls) == 1


def test_api_never_logs_the_query(client, caplog):
    main._suggest_cache.clear()
    with caplog.at_level(logging.DEBUG):
        client.get("/api/sugerencias", params={"q": "palabrasecretaunica"})
        client.get("/api/sugerencias", params={"q": "palabrasecretaunica"})
    assert "palabrasecretaunica" not in caplog.text


def test_api_hostile_q(client):
    for q in ['" OR 1=1', "astro* NEAR(", "\x00\x01 zarz", "<script>alert(1)</script>"]:
        assert client.get("/api/sugerencias", params={"q": q}).status_code == 200


# --- rate limiting ---------------------------------------------------------------------------------------------------
class _Req:
    def __init__(self, path, host="1.2.3.4"):
        self.url = type("u", (), {"path": path})()
        self.client = type("c", (), {"host": host})()
        self.headers = {}


def _hit(mw, path):
    async def ok(_):
        return "ok"
    return asyncio.run(mw.dispatch(_Req(path), ok))


def test_suggestions_have_their_own_budget():
    mw = RateLimitMiddleware(app=None, per_minute=2, suggest_per_minute=5)
    assert [_hit(mw, "/api/sugerencias") for _ in range(5)] == ["ok"] * 5
    assert _hit(mw, "/api/sugerencias").status_code == 429
    # the search budget is untouched by the typing requests...
    assert [_hit(mw, "/buscar") for _ in range(2)] == ["ok", "ok"]
    assert _hit(mw, "/buscar").status_code == 429
    # ...and the other way round: exhausting search does not block suggestions of a fresh client
    mw2 = RateLimitMiddleware(app=None, per_minute=1, suggest_per_minute=3)
    assert _hit(mw2, "/api/buscar") == "ok" and _hit(mw2, "/api/buscar").status_code == 429
    assert _hit(mw2, "/api/sugerencias") == "ok"
    assert all("1.2.3.4" not in k for k in mw.hits)


def test_default_suggestion_budget_is_120():
    assert main.settings.suggest_rate_limit_per_minute == 120


# --- «Quizá buscabas…» on /buscar ----------------------------------------------------------------------------------------
def search(client, **params):
    main._cache.clear()
    r = client.get("/buscar", params=params)
    assert r.status_code == 200
    return r.text


def test_maybe_block_with_zero_results(client):
    html = search(client, q="olivares riego ballenas gigantes")  # only two of four words exist
    assert "data-similar" in html and "Quizá buscabas" in html
    assert 'href="/historia/' in html and "Plan de riego para olivares" in html and "3 medios" in html


def test_maybe_block_with_few_results_skips_stories_already_listed(client):
    # two headlines (< 5) of the very same story are found: it must not be offered again
    html = search(client, q="olivares sur")
    assert "data-similar" not in html


def test_maybe_block_absent_with_many_results_or_exact(client):
    assert "data-similar" not in search(client, q="olivares riego ballenas gigantes", exacta="1")
    assert "data-similar" not in search(client, q="x")  # too short


def test_maybe_block_comes_before_nothing_found_page_end(client):
    html = search(client, q="olivares riego ballenas gigantes")
    assert html.index("No hemos encontrado") < html.index("data-similar")
