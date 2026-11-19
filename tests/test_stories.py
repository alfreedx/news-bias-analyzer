import sqlite3
import time

from app.analysis import Story, make_article
from app.catalog import get_catalog
from app.db import Database
from app.stories import assign_stories, central_article, majority_story

cat = get_catalog()
NOW = int(time.time())


def make_db(tmp_path):
    return Database(tmp_path / "t.sqlite3")


def add(db, rows):
    db.insert_articles([(f"https://x.test/{mid}/{i}-{abs(hash(t))}", mid, t, "", pub) for i, (mid, t, pub) in
                        enumerate(rows)])


def counts(db):
    with db.connect() as c:
        return (c.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM story_articles").fetchone()[0])


def test_migration_is_idempotent_and_upgrades_old_db(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, media_id TEXT NOT NULL, "
                 "title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', published INTEGER NOT NULL, "
                 "fetched INTEGER NOT NULL)")
    conn.commit()
    conn.close()
    for _ in range(3):
        db = Database(path)
    with db.connect() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        idx = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"stories", "story_articles"} <= tables
    assert {"idx_story_articles_article", "idx_stories_updated"} <= idx


def test_create_story_when_two_match_and_skip_singletons(tmp_path):
    db = make_db(tmp_path)
    add(db, [("el-pais", "El Gobierno aprueba la ley de vivienda y alquiler", NOW - 100),
             ("abc", "Gobierno aprueba ley de vivienda y alquiler hoy", NOW - 90),
             ("publico", "Terremoto sacude la costa de Chile esta madrugada", NOW - 80)])
    res = assign_stories(db, cat, now=NOW)
    assert res["created"] == 1
    assert counts(db) == (1, 2)  # the lone earthquake headline is not persisted
    with db.connect() as c:
        story = c.execute("SELECT * FROM stories").fetchone()
    assert story["title"] in ("El Gobierno aprueba la ley de vivienda y alquiler",
                              "Gobierno aprueba ley de vivienda y alquiler hoy")
    assert "vivienda" in story["keywords"].split()


def test_attach_to_existing_story_instead_of_creating(tmp_path):
    db = make_db(tmp_path)
    add(db, [("el-pais", "El Gobierno aprueba la ley de vivienda y alquiler", NOW - 300),
             ("abc", "Gobierno aprueba ley de vivienda y alquiler hoy", NOW - 290)])
    assign_stories(db, cat, now=NOW)
    add(db, [("okdiario", "Aprueba el Gobierno una ley de vivienda y alquiler", NOW - 50)])
    res = assign_stories(db, cat, now=NOW)
    assert res == {"created": 0, "attached": 1, "pending": 0}
    assert counts(db) == (1, 3)
    again = assign_stories(db, cat, now=NOW)  # idempotent: nothing left to do
    assert again["created"] == 0 and again["attached"] == 0 and counts(db) == (1, 3)


def test_old_articles_and_old_stories_are_not_used(tmp_path):
    db = make_db(tmp_path)
    old = NOW - 5 * 86400
    add(db, [("el-pais", "El Gobierno aprueba la ley de vivienda y alquiler", old),
             ("abc", "Gobierno aprueba ley de vivienda y alquiler hoy", old)])
    assert assign_stories(db, cat, now=NOW)["created"] == 0
    with db.connect() as c:  # a stale story is not a candidate for new headlines
        c.execute("INSERT INTO stories(created, updated, title, keywords) VALUES (?, ?, 't', 'gobierno aprueba ley vivienda')",
                  (old, old))
    add(db, [("okdiario", "Gobierno aprueba ley vivienda alquiler", NOW - 10),
             ("publico", "El Gobierno aprueba la ley de vivienda", NOW - 5)])
    res = assign_stories(db, cat, now=NOW)
    assert res["created"] == 1 and res["attached"] == 0


def test_title_is_most_central_headline_and_refreshes(tmp_path):
    db = make_db(tmp_path)
    add(db, [("el-pais", "Gobierno aprueba ley vivienda", NOW - 300),
             ("abc", "Gobierno aprueba ley alquiler", NOW - 290)])
    assign_stories(db, cat, now=NOW)
    add(db, [("okdiario", "Gobierno aprueba ley vivienda alquiler", NOW - 50)])
    assign_stories(db, cat, now=NOW)
    with db.connect() as c:
        row = c.execute("SELECT * FROM stories").fetchone()
    assert row["title"] == "Gobierno aprueba ley vivienda alquiler"  # shares most words with the others
    assert row["updated"] == NOW - 50


def test_central_article_unit():
    mk = lambda mid, t, i: make_article(cat, title=t, url=f"https://x/{i}", summary="", published=i, media_id=mid)  # noqa: E731
    arts = [mk("abc", "Gobierno aprueba ley vivienda", 1), mk("publico", "Gobierno aprueba ley vivienda alquiler", 2),
            mk("okdiario", "Gobierno aprueba ley alquiler", 3)]
    assert central_article(arts) is arts[1]
    assert central_article(arts[:1]) is arts[0]


def test_factcheckers_are_not_assigned(tmp_path):
    db = make_db(tmp_path)
    add(db, [("maldita", "Gobierno aprueba ley vivienda alquiler", NOW - 10),
             ("newtral", "Gobierno aprueba ley vivienda alquiler", NOW - 9)])
    assert assign_stories(db, cat, now=NOW)["created"] == 0


def test_retention_cleans_links_and_empty_stories(tmp_path):
    db = make_db(tmp_path)
    add(db, [("el-pais", "El Gobierno aprueba la ley de vivienda y alquiler", NOW - 100),
             ("abc", "Gobierno aprueba ley de vivienda y alquiler hoy", NOW - 90)])
    assign_stories(db, cat, now=NOW)
    with db.connect() as c:
        c.execute("UPDATE articles SET published = ? WHERE media_id = 'el-pais'", (NOW - 400 * 86400,))
    db.purge_older_than(120)
    assert counts(db) == (1, 1)  # one article left: the story survives with it
    with db.connect() as c:
        c.execute("UPDATE articles SET published = ?", (NOW - 400 * 86400,))
    db.purge_older_than(120)
    assert counts(db) == (0, 0)


def test_story_lookup_helpers(tmp_path):
    db = make_db(tmp_path)
    add(db, [("el-pais", "El Gobierno aprueba la ley de vivienda y alquiler", NOW - 100),
             ("abc", "Gobierno aprueba ley de vivienda y alquiler hoy", NOW - 90)])
    assign_stories(db, cat, now=NOW)
    with db.connect() as c:
        sid = c.execute("SELECT id FROM stories").fetchone()["id"]
    assert db.get_story(sid)["id"] == sid and db.get_story(999) is None
    rows = db.story_article_rows(sid)
    assert len(rows) == 2
    assert set(db.story_ids_for([r["id"] for r in rows] + [12345]).values()) == {sid}


def test_majority_story_mapping():
    mk = lambda i: make_article(cat, title="x", url=f"https://x/{i}", summary="", published=1,  # noqa: E731
                                media_id="abc", article_id=i)
    arts = [mk(1), mk(2), mk(3), mk(4)]
    assert majority_story(arts, {1: 7, 2: 7, 3: 7}) == 7
    assert majority_story(arts, {1: 7, 2: 7}) == 7  # exactly half, at least two
    assert majority_story(arts, {1: 7}) is None
    assert majority_story(arts, {1: 7, 2: 8, 3: 9}) is None
    assert majority_story(arts, {}) is None
    assert Story(arts).story_id is None


def test_second_headline_of_same_outlet_joins_new_story(tmp_path):
    db = make_db(tmp_path)
    add(db, [("el-pais", "El Gobierno aprueba la ley de vivienda y alquiler", NOW - 100),
             ("el-pais", "Gobierno aprueba la ley de vivienda y alquiler en Consejo", NOW - 95),
             ("abc", "Gobierno aprueba ley de vivienda y alquiler hoy", NOW - 90)])
    res = assign_stories(db, cat, now=NOW)
    assert res["created"] == 1 and counts(db) == (1, 3) and res["pending"] == 0
