import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import FEED_FAILURE_LIMIT, Database, feed_pause
from app.feeds import ingest_all
from app.status import feed_status

RSS = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
       b'<item><title>Titular de prueba uno</title><link>https://x.test/a1</link></item></channel></rss>')


def fake_catalog(feeds_by_media):
    media = {mid: SimpleNamespace(id=mid, name=mid.upper(), feeds=feeds) for mid, feeds in feeds_by_media.items()}
    return SimpleNamespace(media=media, news_media=list(media.values()))


def test_feed_pause_is_exponential_and_capped():
    assert feed_pause(0) == feed_pause(FEED_FAILURE_LIMIT - 1) == 0
    assert feed_pause(FEED_FAILURE_LIMIT) == 24 * 3600
    assert feed_pause(FEED_FAILURE_LIMIT + 1) == 48 * 3600
    assert feed_pause(FEED_FAILURE_LIMIT + 2) == 96 * 3600
    assert feed_pause(500) == 7 * 24 * 3600


def test_dead_feed_is_paused_after_20_failures_and_recovers(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    url = "https://dead.test/feed"
    for i in range(FEED_FAILURE_LIMIT - 1):
        db.save_feed_state(url, "m", etag=None, last_modified=None, status="503", items=0, success=False)
        assert db.feed_due(db.get_feed_state(url))
    db.save_feed_state(url, "m", etag=None, last_modified=None, status="503", items=0, success=False)
    st = db.get_feed_state(url)
    assert st["failures"] == FEED_FAILURE_LIMIT
    assert not db.feed_due(st)
    assert st["next_attempt"] - int(time.time()) in range(24 * 3600 - 5, 24 * 3600 + 5)
    assert db.feed_due(st, now=st["next_attempt"] + 1)
    # one more failure after the pause doubles it
    db.save_feed_state(url, "m", etag=None, last_modified=None, status="503", items=0, success=False)
    assert db.get_feed_state(url)["next_attempt"] - int(time.time()) > 47 * 3600
    # a success clears everything
    db.save_feed_state(url, "m", etag=None, last_modified=None, status="200", items=3, success=True)
    st = db.get_feed_state(url)
    assert st["failures"] == 0 and st["next_attempt"] is None and db.feed_due(st)


def test_old_database_gets_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE feed_state (url TEXT PRIMARY KEY, media_id TEXT NOT NULL, etag TEXT, "
                 "last_modified TEXT, last_attempt INTEGER, last_success INTEGER, last_status TEXT, "
                 "last_items INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO feed_state(url, media_id) VALUES ('https://a.test/f', 'm')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.get_feed_state("https://a.test/f")["failures"] == 0


def test_ingest_skips_paused_feeds_and_counts_failures(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = fake_catalog({"ok": ["https://ok.test/rss"], "dead": ["https://dead.test/rss"]})
    for _ in range(FEED_FAILURE_LIMIT):
        db.save_feed_state("https://dead.test/rss", "dead", etag=None, last_modified=None, status="500",
                           items=0, success=False)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=RSS)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(ingest_all(cat, db, client=client)) == 1
    assert seen == ["https://ok.test/rss"]
    assert db.get_feed_state("https://ok.test/rss")["failures"] == 0


def test_ingest_limits_per_host_and_global_concurrency(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    feeds = {f"m{i}": [f"https://same.test/{i}/rss"] for i in range(6)}
    feeds |= {f"o{i}": [f"https://other{i}.test/rss"] for i in range(10)}
    cat = fake_catalog(feeds)
    live = {"all": 0, "max_all": 0, "per": {}, "max_per": {}}

    async def handler(request):
        host = request.url.host
        live["all"] += 1
        live["per"][host] = live["per"].get(host, 0) + 1
        live["max_all"] = max(live["max_all"], live["all"])
        live["max_per"][host] = max(live["max_per"].get(host, 0), live["per"][host])
        await asyncio.sleep(0.02)
        live["all"] -= 1
        live["per"][host] -= 1
        return httpx.Response(200, content=RSS)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(ingest_all(cat, db, concurrency=4, client=client))
    assert live["max_per"]["same.test"] <= 2
    assert live["max_all"] <= 4
    assert live["max_all"] > 2  # it really runs in parallel


def test_default_concurrency_is_configurable():
    from app.config import settings
    assert settings.ingest_concurrency == 8


def test_feed_status_counts_and_no_personal_data(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    cat = fake_catalog({"a": ["https://a.test/f"], "b": ["https://b.test/f"], "c": ["https://c.test/f"],
                        "d": ["https://d.test/f"]})
    db.save_feed_state("https://a.test/f", "a", etag=None, last_modified=None, status="200", items=5, success=True)
    db.save_feed_state("https://b.test/f", "b", etag=None, last_modified=None, status="404", items=0, success=False)
    for _ in range(FEED_FAILURE_LIMIT):
        db.save_feed_state("https://c.test/f", "c", etag=None, last_modified=None, status="500", items=0,
                           success=False)
    s = feed_status(cat, db)
    assert s["feeds_total"] == 4
    assert s["counts"] == {"active": 1, "failing": 1, "paused": 1, "pending": 1}
    assert [r.outlet_id for r in s["problems"]] == ["c", "b", "d"]  # paused first, then failing, then pending
    assert s["last_ingest"] and s["index_bytes"] > 0


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_estado_page(client):
    now = int(time.time())
    main.db.insert_articles([("https://x.test/estado1", "el-pais", "Titular de estado", "", now)])
    r = client.get("/estado")
    assert r.status_code == 200
    html = r.text
    assert "Estado del servicio" in html and "Feeds en el catálogo" in html
    assert "style=" not in html and "<script>" not in html
    assert "set-cookie" not in r.headers
    assert 'href="/estado"' in client.get("/").text  # linked from the footer
    assert "testclient" not in html  # nothing about the visitor
