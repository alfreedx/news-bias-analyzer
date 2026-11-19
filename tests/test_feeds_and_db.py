import time

from app.analysis import cluster, coverage, make_article
from app.catalog import get_catalog
from app.db import Database, build_fts_query
from app.feeds import parse_feed
from app.text import clean_text, headline_signals

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>El Gobierno aprueba la ley de vivienda</title><link>https://elpais.com/a.html</link>
<description>&lt;p&gt;Texto &lt;b&gt;con&lt;/b&gt; html&lt;/p&gt;</description><pubDate>Wed, 18 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Enlace malicioso</title><link>javascript:alert(1)</link></item>
<item><title>Noticia - Medio</title><link>https://news.google.com/x</link><source url="https://www.eldiario.es">elDiario.es</source></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>Titular atom</title><link rel="alternate" href="https://www.elconfidencial.com/a/"/>
<updated>2026-11-18T09:00:00Z</updated><summary>Resumen</summary></entry></feed>"""

XXE = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<rss><channel><item><title>&x;</title><link>https://a.es</link></item></channel></rss>"""

BOMB = b"""<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><rss><channel><item><title>&c;</title><link>https://a.es</link></item></channel></rss>"""


def test_parse_rss_strips_html_and_rejects_bad_links():
    items = parse_feed(RSS)
    assert [i.title for i in items] == ["El Gobierno aprueba la ley de vivienda", "Noticia - Medio"]
    assert items[0].summary == "Texto con html"
    assert items[1].source_url == "https://www.eldiario.es"


def test_parse_atom():
    items = parse_feed(ATOM)
    assert len(items) == 1 and items[0].url == "https://www.elconfidencial.com/a/"


def test_xml_attacks_are_refused():
    assert parse_feed(XXE) == []
    assert parse_feed(BOMB) == []
    assert parse_feed(b"not xml at all") == []


def test_future_dates_are_clamped():
    now = int(time.time())
    future = RSS.replace(b"Wed, 18 Nov 2026 10:00:00 GMT", b"Fri, 01 Jan 2100 00:00:00 GMT")
    assert parse_feed(future, now=now)[0].published == now


def test_fts_query_cannot_inject_syntax():
    q = build_fts_query('" OR title:* NEAR( vivienda) -- ^')
    assert q is not None
    for token in q.split(" AND "):
        assert token.startswith('"')
    assert build_fts_query("   ") is None
    assert build_fts_query("la de el") is not None  # only stopwords: still searchable


def test_db_search_roundtrip(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    now = int(time.time())
    db.insert_articles([
        ("https://elpais.com/1", "el-pais", "La ley de vivienda llega al Congreso", "", now),
        ("https://abc.es/2", "abc", "El Congreso debate la ley de vivienda", "", now),
        ("https://abc.es/3", "abc", "Resultados de fútbol", "", now),
    ])
    assert db.insert_articles([("https://abc.es/3", "abc", "dup", "", now)]) == 0
    rows = db.search("viviendas congreso")
    assert {r["url"] for r in rows} == {"https://elpais.com/1", "https://abc.es/2"}
    assert db.search("futbol")[0]["url"] == "https://abc.es/3"  # accent-insensitive


def test_clustering_and_coverage():
    cat = get_catalog()
    now = int(time.time())
    arts = [
        make_article(cat, title="El Congreso aprueba la ley de vivienda con el apoyo de los socios",
                     url="https://elpais.com/1", summary="", published=now, media_id="el-pais"),
        make_article(cat, title="El Congreso aprueba la ley de vivienda entre críticas del PP",
                     url="https://abc.es/1", summary="", published=now, media_id="abc"),
        make_article(cat, title="Récord de turistas en Canarias", url="https://x.es/1", summary="", published=now),
    ]
    stories = cluster(arts)
    assert len(stories) == 2
    assert stories[0].outlets == 2 or stories[1].outlets == 2
    cov = coverage(arts)
    assert cov.total == 3 and cov.rated == 2
    assert arts[2].source_name == "Medio desconocido"


def test_headline_signals():
    assert "Titular tipo cebo" in headline_signals("No vas a creer lo que pasó en el pleno")
    assert "Lenguaje cargado" in headline_signals("Escándalo en el Congreso")
    assert headline_signals("El Congreso aprueba los presupuestos") == []


def test_clean_text_truncates():
    assert clean_text("<b>a</b> " * 500, max_len=20).endswith("…")


# --- bylines ---------------------------------------------------------------------
BYLINES = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>t</title>
<item><title>Uno</title><link>https://a.es/1</link><dc:creator>Por   Ana  Pérez </dc:creator></item>
<item><title>Dos</title><link>https://a.es/2</link><author>jose@a.es (José Ruiz)</author></item>
<item><title>Tres</title><link>https://a.es/3</link><author>redaccion@a.es</author></item>
<item><title>Cuatro</title><link>https://a.es/4</link><dc:creator>Redacción</dc:creator></item>
<item><title>Cinco</title><link>https://a.es/5</link><dc:creator>EFE</dc:creator></item>
<item><title>Seis</title><link>https://a.es/6</link><dc:creator>Luis Gil</dc:creator><dc:creator>Eva Sanz</dc:creator></item>
</channel></rss>""".encode()  # noqa: UP012

ATOM_AUTHOR = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>T</title><link rel="alternate" href="https://b.es/a"/><updated>2026-11-18T09:00:00Z</updated>
<author><name>Marta Vidal</name><email>m@b.es</email></author></entry></feed>"""


def test_author_parsing_rss_dc_creator_and_email_stripping():
    by = {i.title: i.author for i in parse_feed(BYLINES)}
    assert by["Uno"] == "Ana Pérez"
    assert by["Dos"] == "José Ruiz"
    assert by["Tres"] is None and by["Cuatro"] is None
    assert by["Cinco"] == "EFE"  # named agencies are kept
    assert by["Seis"] == "Luis Gil, Eva Sanz"


def test_author_parsing_atom():
    assert parse_feed(ATOM_AUTHOR)[0].author == "Marta Vidal"


def test_clean_author_rules():
    from app.feeds import clean_author
    assert clean_author("El País", "El País") is None
    assert clean_author("x" * 300) is not None and len(clean_author("word " * 60)) <= 120
    assert clean_author("Agencias") is None
    assert clean_author("https://a.es/autor") is None


def test_migration_adds_author_to_old_schema(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE articles (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
        media_id TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        published INTEGER NOT NULL, fetched INTEGER NOT NULL);
        INSERT INTO articles(url, media_id, title, published, fetched) VALUES ('https://a.es/1','abc','Viejo',1,1);""")
    conn.commit()
    conn.close()
    db = Database(path)
    Database(path)  # idempotent
    now = int(time.time())
    assert db.insert_articles([("https://a.es/2", "abc", "Nuevo", "", now, "Ana Pérez")]) == 1
    assert db.insert_articles([("https://a.es/1", "abc", "Viejo", "", now, "Luis Gil")]) == 0  # backfills author
    assert [r["author"] for r in db.by_author("Luis Gil")] == ["Luis Gil"]
    assert db.author_outlets("Ana Pérez")[0]["n"] == 1
    assert db.by_author("ana pérez") == []  # exact match only


def test_clean_author_newsroom_and_comma_spacing():
    from app.feeds import clean_author
    assert clean_author("Redacción El Debate", "El Debate") is None
    assert clean_author("Jesús García Bueno,Rebeca Carranco") == "Jesús García Bueno, Rebeca Carranco"
