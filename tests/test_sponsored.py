"""Sponsored content only as marked by the outlet itself (app/sponsored.py, data/sponsored_rules.json)."""

import time

import pytest
from fastapi.testclient import TestClient

from app import main, sponsored
from app.analysis import article_from_row, cluster, coverage, make_article
from app.catalog import get_catalog
from app.consensus import build_voices, consensus
from app.db import Database
from app.feeds import join_categories, parse_feed
from app.stories import assign_stories

CAT = get_catalog()


def rss(items: str) -> bytes:
    return (f'<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            f"<title>t</title>{items}</channel></rss>").encode()


# --- parsing -----------------------------------------------------------------
def test_categories_from_rss_dc_subject_and_atom():
    body = rss('<item><title>A</title><link>https://x.es/a</link><category>Economía</category>'
               '<category><![CDATA[Contenido patrocinado]]></category><dc:subject>Branded</dc:subject>'
               '<category>Economía</category></item>')
    assert parse_feed(body)[0].categories == ("Economía", "Contenido patrocinado", "Branded")
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link href="https://x.es/t"/>'
            b'<category term="sponsored" label="Contenido de marca"/><category term="otra"/></entry></feed>')
    assert parse_feed(atom)[0].categories == ("Contenido de marca", "otra")
    assert parse_feed(rss("<item><title>A</title><link>https://x.es/a</link></item>"))[0].categories == ()


def test_categories_are_capped():
    many = "".join(f"<category>c{i} " + "x" * 100 + "</category>" for i in range(40))
    cats = parse_feed(rss(f"<item><title>A</title><link>https://x.es/a</link>{many}</item>"))[0].categories
    assert len(cats) == 12 and all(len(c) <= 60 for c in cats)
    assert len(join_categories(cats)) <= 400
    assert join_categories(()) is None


# --- rules -------------------------------------------------------------------
def test_rules_file_is_public_and_well_formed():
    meta = sponsored.rules_meta()
    assert meta["version"] == 1 and meta["updated"] and meta["count"] > 10
    for r in sponsored.load_rules():
        assert r.field in sponsored.FIELDS and r.label and r.pattern


@pytest.mark.parametrize("cats,url,title,expected", [
    ("Economía | Contenido patrocinado", "https://x.es/a/b.html", "Titular", "category-patrocinado"),
    ("Branded Content", "https://x.es/a/b.html", "Titular", "category-branded"),
    ("ED Branded", "https://x.es/a/b.html", "Titular", "category-branded"),
    ("Publirreportaje", "https://x.es/a/b.html", "Titular", "category-publirreportaje"),
    ("CONTENIDO DE MARCA", "https://x.es/a/b.html", "Titular", "category-contenido-de-marca"),
    (None, "https://x.es/gastronomia/branded/serie/2026/a.html", "Titular", "path-branded"),
    (None, "https://x.es/patrocinado/a.html", "Titular", "path-patrocinado"),
    (None, "https://x.es/contenido-patrocinado/a.html", "Titular", "path-patrocinado"),
    (None, "https://x.es/a/b.html", "Patrocinado: una oferta", "title-patrocinado"),
    (None, "https://x.es/a/b.html", "[Publicidad] Una oferta", "title-bracket-publicidad"),
    (None, "https://x.es/a/b.html", "  PUBLIRREPORTAJE: algo", "title-publirreportaje"),
])
def test_each_rule_type_matches(cats, url, title, expected):
    assert sponsored.match("el-pais", url, title, cats) == expected


@pytest.mark.parametrize("cats,url,title", [
    ("Publicidad", "https://theobjective.com/medios/2026/a/", "El sector del entretenimiento y los medios crece"),
    ("Salud pública | Ayudas públicas | Marcas blancas", "https://x.es/salud/a.html", "Titular"),
    (None, "https://www.lasprovincias.es/deportes/valencia-bc/a.html", "Valencia Basket gana"),
    (None, "https://x.es/ofertas/chollos/a.html", "Chollos de Black Friday"),
    ("Especiales | Ofertas", "https://x.es/especiales/a.html", "Especial Trump"),
    (None, "https://x.es/a/b.html", "Publicidad: el sector crece un 5 %"),
    (None, "https://x.es/a/branded-por-que-las-marcas.html", "El último segmento (el slug) no cuenta"),
    (None, "https://x.es/a/b.html", "El patrocinado equipo gana"),
])
def test_false_positive_guard(cats, url, title):
    assert sponsored.match("the-objective", url, title, cats) is None


def test_outlet_scoped_rule_only_applies_to_that_outlet():
    url = "https://www.20minutos.es/bc/puleva-cambia-ritmo_7062218_5.html"
    assert sponsored.match("20minutos", url, "Titular") == "outlet-20minutos-bc"
    assert sponsored.match("el-pais", url, "Titular") is None


def test_evidence_texts():
    ev = sponsored.evidence("category-patrocinado", "https://x.es/a.html", "T", "Economía | Contenido patrocinado")
    assert ev == "Categoría RSS «Contenido patrocinado»"
    assert sponsored.evidence("path-branded", "https://x.es/gastro/branded/a/b.html", "T", None) == "Ruta /branded/"
    assert sponsored.evidence(None, "u", "t", None) is None


# --- database ------------------------------------------------------------------
def make_db(tmp_path):
    return Database(tmp_path / "t.sqlite3")


def test_insert_marks_and_reevaluates_on_reingest(tmp_path):
    db = make_db(tmp_path)
    now = int(time.time())
    url = "https://elpais.com/economia/a.html"
    db.insert_articles([(url, "el-pais", "Titular uno", "", now, None, "Economía | Contenido patrocinado")])
    with db.connect() as c:
        row = c.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
    assert row["sponsored"] == "category-patrocinado" and "patrocinado" in row["categories"]
    # the outlet drops the mark: the next read of the feed clears it
    db.insert_articles([(url, "el-pais", "Titular uno", "", now, None, "Economía")])
    with db.connect() as c:
        assert c.execute("SELECT sponsored FROM articles WHERE url = ?", (url,)).fetchone()[0] is None
    # 5- and 6-tuples keep working
    assert db.insert_articles([("https://x.es/z", "abc", "Z", "", now), ("https://x.es/y", "abc", "Y", "", now, "Ana")]) == 2


def test_migration_adds_columns_and_retroactive_url_title_rules_are_idempotent(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = Database(path)  # build a complete database (FTS, triggers), then take it back to the previous schema
    with db.connect() as c:
        c.executemany("INSERT INTO articles(url, media_id, title, published, fetched) VALUES (?, 'el-pais', ?, 1, 1)", [
            ("https://elpais.com/x/branded/s/a.html", "Un titular"),
            ("https://elpais.com/x/a.html", "Patrocinado: una oferta"),
            ("https://elpais.com/x/b.html", "Titular normal")])
        c.execute("UPDATE articles SET sponsored = NULL")
        c.execute("ALTER TABLE articles DROP COLUMN categories")
        c.execute("ALTER TABLE articles DROP COLUMN sponsored")
    for _ in range(2):  # a second start changes nothing
        db = Database(path)
        with db.connect() as c:
            marks = {r["url"]: r["sponsored"] for r in c.execute("SELECT url, sponsored FROM articles")}
            cols = {r["name"] for r in c.execute("PRAGMA table_info(articles)")}
        assert {"categories", "sponsored"} <= cols
        assert marks == {"https://elpais.com/x/branded/s/a.html": "path-branded",
                         "https://elpais.com/x/a.html": "title-patrocinado", "https://elpais.com/x/b.html": None}
    with db.connect() as c:
        assert db.reevaluate_sponsored(c) == 0


def test_marked_articles_leave_persistent_stories(tmp_path):
    db = make_db(tmp_path)
    now = int(time.time())
    base = "El Congreso aprueba la ley de vivienda con el apoyo de los socios"
    db.insert_articles([("https://elpais.com/1", "el-pais", base, "", now),
                        ("https://www.abc.es/2", "abc", base + " y críticas del PP", "", now),
                        ("https://elpais.com/branded/3", "el-pais", base + " según una marca", "", now)])
    out = assign_stories(db, CAT)
    assert out["created"] == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM story_articles").fetchone()[0] == 2  # never the branded one
        sid = c.execute("SELECT story_id FROM story_articles").fetchone()[0]
    assert all(r["sponsored"] is None for r in db.story_article_rows(sid))
    # a mark that appears later on an already assigned article removes it from the story
    db.insert_articles([("https://www.abc.es/2", "abc", base + " y críticas del PP", "", now, None, "Branded content")])
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM story_articles").fetchone()[0] == 1  # only the unmarked one is left


# --- exclusion from analysis ---------------------------------------------------
def art(mid, title, n, sponsored_id=None):
    return make_article(CAT, title=title, url=f"https://x.test/{mid}/{n}", summary="", published=1000 + n,
                        media_id=mid, sponsored=sponsored_id)


def test_sponsored_never_cluster_count_or_speak():
    t = "El Gobierno aprueba la ley de vivienda y alquiler en el Consejo"
    regular = [art("el-pais", t, 1), art("abc", t + " hoy", 2), art("eldiario-es", t + " ayer", 3)]
    marked = art("okdiario", t + " patrocinada", 4, "path-branded")
    stories = cluster([*regular, marked])
    assert len(stories) == 1 and marked not in stories[0].articles and stories[0].outlets == 3
    cov = coverage([*regular, marked])
    assert cov.total == 3 and "OKDIARIO" not in [n for _, n, _ in cov.by_media]
    assert all(marked not in v.articles for v in build_voices([*regular, marked]))
    assert consensus([*regular, marked]).voices == consensus(regular).voices


# --- pages ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    now = int(time.time())
    main.db.insert_articles([
        ("https://elpais.com/p1", "el-pais", "Los alquileres turísticos suben en Cádiz", "Resumen", now),
        ("https://www.abc.es/p2", "abc", "Los alquileres turísticos suben en Cádiz según datos", "", now),
        ("https://elpais.com/cultura/branded/marca/2026/p3.html", "el-pais", "Alquileres turísticos y seguros de hogar",
         "", now, "Ana Pérez", "Cultura"),
        ("https://www.20minutos.es/p4", "20minutos", "Alquileres turísticos: hipoteca ideal", "", now, None,
         "Branded Content"),
    ])
    with TestClient(main.app) as c:
        yield c


def test_search_lists_sponsored_apart_in_collapsed_details(client):
    html = client.get("/buscar", params={"q": "alquileres turísticos"}).text
    assert "2 piezas marcadas como patrocinadas por su medio" in html
    assert '<details class="sponsored-box">' in html  # not `open`
    head, _, tail = html.partition('<details class="sponsored-box">')
    assert "Marcado como patrocinado por el medio" not in head  # only inside the details block
    assert 'title="Ruta /branded/"' in tail and "Categoría RSS «Branded Content»" in tail
    assert "no patrocinado" not in html.lower()
    assert "seguros de hogar" not in head


def test_no_mark_without_signal(client):
    html = client.get("/buscar", params={"q": "Cádiz"}).text
    assert "patrocinad" not in html.lower()
    assert "sponsored-box" not in html.split("<main")[-1]


def test_methodology_and_glossary_and_api(client):
    m = client.get("/metodologia").text
    assert 'id="patrocinado"' in m and "sponsored_rules.json" in m and "Ley 34/1988" in m
    assert "no garantiza que una pieza no sea publicidad" in m and "issues/new" in m
    assert 'id="patrocinado"' in client.get("/como-leer").text
    data = client.get("/api/buscar", params={"q": "alquileres turísticos"}).json()
    assert len(data["sponsored"]) == 2 and data["coverage"]["total"] == 2


def test_article_from_row_carries_evidence(client):
    with main.db.connect() as c:
        row = c.execute("SELECT * FROM articles WHERE url = 'https://www.20minutos.es/p4'").fetchone()
    a = article_from_row(CAT, row)
    assert a.sponsored == "category-branded" and a.sponsored_evidence == "Categoría RSS «Branded Content»"
