"""Equivalent words in the search (data/synonyms.json), story-level matching, fallback and the page that explains it."""

import importlib.util
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, synonyms
from app.db import Database, build_fts_query, in_lead
from app.synonyms import MAX_SYNONYMS, key_of, parse_units

ROOT = Path(__file__).resolve().parent.parent
NOW = int(time.time())


def make_db(tmp_path, rows, stories=()):
    """rows: (url, media_id, title[, summary]); stories: lists of urls forming a persisted story."""
    db = Database(tmp_path / "s.sqlite3")
    db.insert_articles([(r[0], r[1], r[2], r[3] if len(r) > 3 else "", NOW - i * 60) for i, r in enumerate(rows)])
    with db.connect() as c:
        for urls in stories:
            sid = c.execute("INSERT INTO stories(created, updated, title, keywords) VALUES (?, ?, 't', '')",
                            (NOW, NOW)).lastrowid
            for u in urls:
                aid = c.execute("SELECT id FROM articles WHERE url = ?", (u,)).fetchone()[0]
                c.execute("INSERT INTO story_articles(story_id, article_id) VALUES (?, ?)", (sid, aid))
    return db


# --- the data file -------------------------------------------------------------------------------------------
def test_synonyms_file_is_valid_and_reviewable():
    raw = json.loads((ROOT / "data" / "synonyms.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1 and re.fullmatch(r"\d{4}-\d\d-\d\d", raw["updated"])
    groups = [g["words"] if isinstance(g, dict) else g for g in raw["groups"]]
    assert 80 <= len(groups) <= 120
    for words in groups:
        assert len(words) >= 2 and len({key_of(w) for w in words}) == len(words), words
        assert all(w == w.lower() and w.isascii() for w in words), words  # folded: no accents, no capitals
    assert len({tuple(sorted(w)) for w in groups}) == len(groups)
    assert synonyms.meta()["count"] == len(groups)


# Loaded or partisan words that must never be treated as interchangeable (neutrality guard).
LOADED_PAIRS = [
    ("okupa", "inquilino"), ("okupa", "ocupante"), ("ilegal", "irregular"), ("golpe", "proceso"),
    ("terrorista", "militante"), ("terrorista", "activista"), ("regimen", "gobierno"),
    ("invasion", "llegada"), ("invasion", "entrada"), ("aborto", "interrupcion voluntaria del embarazo"),
    ("eutanasia", "muerte digna"), ("menas", "menores migrantes"), ("desahucio", "desalojo"),
    ("violencia de genero", "violencia machista"), ("crisis climatica", "cambio climatico"),
    ("emergencia climatica", "cambio climatico"), ("separatista", "independentista"),
    ("independentista", "nacionalista"), ("facha", "conservador"), ("ultraderecha", "derecha"),
    ("ultraizquierda", "izquierda"), ("progre", "progresista"), ("bulo", "mentira"), ("masacre", "operacion"),
    ("genocidio", "guerra"), ("ocupacion", "presencia"), ("colono", "residente"), ("antifascista", "radical"),
]


def test_no_loaded_pair_shares_a_group():
    syn = synonyms.load()
    for a, b in LOADED_PAIRS:
        shared = set(syn.index.get(key_of(a), ())) & set(syn.index.get(key_of(b), ()))
        assert not shared, f"{a!r} and {b!r} must not be grouped"


# --- expansion --------------------------------------------------------------------------------------------------
def test_expansion_folds_accents_and_plurals():
    fts = build_fts_query("Subvención Sánchez")
    assert fts.startswith('("subvencion"* OR ') and '"bono"' in fts and '"bonos"' in fts
    assert fts.endswith(' AND "sanchez"*')
    assert '"bono"' in build_fts_query("ayudas")  # «ayudas» is reached through its singular
    assert '"subvencion"' in build_fts_query("ayudas")


def test_expansion_phrases_and_stopwords_inside_a_phrase():
    fts = build_fts_query("partido popular vivienda")
    assert '("partido"* AND "popular"*)' in fts and '"pp"' in fts and fts.endswith('AND "vivienda"*')
    units = parse_units("lo que dice el partido popular")
    assert any(u.literal == ("partido", "popular") and "pp" in u.synonyms for u in units)
    assert '"union europea"' in build_fts_query("ue")
    assert '"ee uu"' in build_fts_query("estados unidos")


def test_exact_mode_never_expands():
    assert build_fts_query("subvencion sanchez", exact=True) == '"subvencion"* AND "sanchez"*'


def test_expansion_is_capped():
    q = "subvencion bono ayuda hackeo ipc gripe covid movil"  # many words, each with many equivalents
    units = parse_units(q)
    assert sum(len(u.synonyms) for u in units) <= MAX_SYNONYMS <= 40
    assert all(u.synonyms for u in units)  # shared in turns: nobody is left without any
    terms = re.findall(r'"[^"]*"', build_fts_query(q))
    assert len(terms) <= 8 + 2 * MAX_SYNONYMS + 8


@pytest.mark.parametrize("hostile", ['" OR 1 NEAR/ col:', 'bono" OR title:* NEAR(a b) -- ^', "ayuda) OR (secreto", "*:* {xy}"])
def test_hostile_input_stays_inside_quotes(hostile):
    fts = build_fts_query(hostile)
    assert fts is not None
    outside = re.sub(r'"[\w ]*"\*?', "", fts)
    assert re.fullmatch(r"[()\sA-Z]*", outside), outside  # only parentheses, AND / OR remain: no user text
    assert "NEAR" not in outside and ":" not in fts and "^" not in fts and "-" not in fts
    assert all(set(t) <= set('"') | set("abcdefghijklmnopqrstuvwxyz0123456789_ ") for t in re.findall(r'"[^"]*"', fts))


def test_in_lead_accepts_equivalent_words():
    lead = "El presidente presenta una paga de 200 euros para los jóvenes que practiquen deporte en el curso"
    assert in_lead("subvencion jovenes", lead)
    assert not in_lead("subvencion jovenes", lead, exact=True)


# --- ranking and the real example ----------------------------------------------------------------------------------
BONO = [
    ("https://a.es/diario-bono", "el-pais", "Sánchez anuncia un bono joven de deporte de 200 euros al año"),
    ("https://b.es/paga", "el-mundo", "Una paga de 200 euros a los jóvenes para hacer deporte, la promesa de Sánchez"),
    ("https://c.es/deportivo", "abc", "Un bono deportivo que costará 908 millones, según el Gobierno"),
]


def test_real_example_subvencion_sanchez(tmp_path):
    db = make_db(tmp_path, [*BONO, ("https://d.es/otro", "abc", "Sánchez viaja a Bruselas")])
    assert db.search("subvencion sanchez", exact=True) == []
    urls = [r["url"] for r in db.search("subvencion sanchez")]
    assert set(urls[:2]) == {BONO[0][0], BONO[1][0]}
    assert "https://d.es/otro" not in urls and "https://c.es/deportivo" not in urls  # no «sánchez» in the last two


def test_literal_words_rank_above_equivalent_only(tmp_path):
    rows = [(f"https://x.es/bono{i}", "abc", f"Un bono para el comercio número {i}") for i in range(3)]
    rows.append(("https://x.es/lit", "abc", "Nueva subvención para el comercio local"))
    db = make_db(tmp_path, rows)
    found = db.search("subvencion comercio")
    assert found[0]["url"] == "https://x.es/lit" and len(found) == 4
    assert found[0]["rank"] < found[1]["rank"] <= 0  # more negative = better; the literal match leads
    assert found[0]["missing"] == 0 and all(r["missing"] == 1 for r in found[1:])


def test_equivalent_words_report_which_ones_stood_in(tmp_path):
    db = make_db(tmp_path, BONO)
    res = db.search_full("subvencion deporte")
    assert "bono" in res.synonyms_used
    assert db.search_full("subvencion deporte", exact=True).synonyms_used == []


# --- story level ------------------------------------------------------------------------------------------------
SPLIT = [
    ("https://a.es/1", "el-pais", "Sánchez presenta su plan para los jóvenes", ""),
    ("https://b.es/2", "el-espanol", "Una paga de 200 euros para los jóvenes, la novedad", ""),
]


def test_story_level_returns_story_when_words_are_spread_over_its_articles(tmp_path):
    db = make_db(tmp_path, SPLIT, stories=[[r[0] for r in SPLIT]])
    assert db.search("subvencion sanchez", exact=True) == []
    res = db.search_full("subvencion sanchez")
    assert {r["url"] for r in res.rows} == {"https://a.es/1", "https://b.es/2"}
    (note,) = res.story_notes.values()
    assert {"word": "sanchez", "equivalent_of": None, "media": ["el-pais"]} in note
    assert {"word": "paga", "equivalent_of": "subvencion", "media": ["el-espanol"]} in note


def test_story_level_needs_every_word_inside_one_story(tmp_path):
    rows = [
        ("https://a.es/1", "el-pais", "Sánchez presenta su plan", ""),
        ("https://a.es/1b", "abc", "Sánchez defiende el plan", ""),
        ("https://b.es/2", "el-espanol", "Una paga de 200 euros", ""),
        ("https://b.es/2b", "el-mundo", "La paga de 200 euros llega", ""),
    ]
    db = make_db(tmp_path, rows, stories=[["https://a.es/1", "https://a.es/1b"], ["https://b.es/2", "https://b.es/2b"]])
    res = db.search_full("subvencion sanchez")
    assert res.rows == [] and res.story_notes == {}


def test_story_level_ignores_one_article_stories_and_can_be_turned_off(tmp_path):
    db = make_db(tmp_path, SPLIT, stories=[["https://a.es/1"]])
    assert db.search_full("subvencion sanchez").rows == []
    (tmp_path / "x").mkdir()
    db = make_db(tmp_path / "x", SPLIT, stories=[[r[0] for r in SPLIT]])
    assert db.search_full("subvencion sanchez", exact=True).rows == []


def test_story_level_prefers_stories_with_two_words_in_one_article(tmp_path):
    rows = [
        # story 1: no article has two of the three words
        ("https://s1.es/a", "abc", "Sánchez recibe a los alcaldes", ""),
        ("https://s1.es/b", "abc", "Un bono para el turismo llega", ""),
        ("https://s1.es/c", "abc", "El comercio local pide calma ante la tormenta", ""),
        # story 2: one article already has two
        ("https://s2.es/a", "abc", "Sánchez anuncia un bono", ""),
        ("https://s2.es/b", "abc", "El comercio local se prepara", ""),
    ]
    db = make_db(tmp_path, rows, stories=[[r[0] for r in rows[:3]], [r[0] for r in rows[3:]]])
    res = db.search_full("subvencion sanchez comercio")
    assert len(res.story_notes) == 2
    first = next(iter(res.story_notes))
    with db.connect() as c:
        urls = {r[0] for r in c.execute("SELECT a.url FROM story_articles sa JOIN articles a ON a.id = sa.article_id "
                                        "WHERE sa.story_id = ?", (first,))}
    assert "https://s2.es/a" in urls


# --- fallback and suggestions ----------------------------------------------------------------------------------------
def test_zero_results_fall_back_to_all_but_one_word(tmp_path):
    rows = [("https://a.es/1", "abc", "Sánchez visita Soria", ""), ("https://a.es/2", "abc", "Una tormenta cubre Soria", ""),
            ("https://a.es/3", "abc", "Sánchez y la tormenta", "")]
    db = make_db(tmp_path, rows)
    assert db.search_full("sanchez tormenta soria").rows == []  # no article has all three
    res = db.search_full("sanchez tormenta soria", fallback=True)
    assert res.approx and {r["url"] for r in res.rows} == {"https://a.es/1", "https://a.es/2", "https://a.es/3"}
    two = [r["url"] for r in res.rows[:2]]
    assert "https://a.es/3" in two or "https://a.es/1" in two  # best-matching first (two of three words)
    assert not db.search_full("sanchez ballena", fallback=True).approx or db.search_full("sanchez ballena", fallback=True).rows


def test_suggest_removals_counts_each_single_word_removed(tmp_path):
    rows = [("https://a.es/1", "abc", "Sánchez visita Soria", ""), ("https://a.es/2", "abc", "Sánchez en Madrid", ""),
            ("https://a.es/3", "abc", "Una tormenta cubre Soria", "")]
    db = make_db(tmp_path, rows)
    sug = db.suggest_removals("sánchez tormenta")
    assert sug == [{"query": "tormenta", "dropped": "sánchez", "count": 1}, {"query": "sánchez", "dropped": "tormenta", "count": 2}] \
        or sug == [{"query": "sánchez", "dropped": "tormenta", "count": 2}, {"query": "tormenta", "dropped": "sánchez", "count": 1}]
    assert sug[0]["count"] == 2
    assert db.suggest_removals("sánchez") == []


# --- the page --------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    now = NOW
    main.db.insert_articles([
        ("https://elpais.com/zz1", "el-pais", "Zorrilla anuncia un plan de pádel para jóvenes con 200 euros", "", now),
        ("https://elespanol.com/zz2", "el-espanol", "Una paga de pádel para jóvenes con 200 euros anuncia el Gobierno", "", now),
        ("https://abc.es/zz3", "abc", "Zorrilla visita un club de remo", "", now),
    ])
    with main.db.connect() as c:
        ids = [c.execute("SELECT id FROM articles WHERE url = ?", (u,)).fetchone()[0]
               for u in ("https://elpais.com/zz1", "https://elespanol.com/zz2")]
        sid = c.execute("INSERT INTO stories(created, updated, title, keywords) VALUES (?, ?, 'x', '')", (now, now)).lastrowid
        c.executemany("INSERT INTO story_articles(story_id, article_id) VALUES (?, ?)", [(sid, i) for i in ids])
    with TestClient(main.app) as c:
        yield c


def page(client, **params):
    main._cache.clear()
    r = client.get("/buscar", params=params)
    assert r.status_code == 200
    return r.text


def test_page_explains_equivalent_words_and_story_level(client):
    html = page(client, q="subvención zorrilla")
    assert "También hemos buscado:" in html and "paga" in html and "(palabras equivalentes)" in html
    assert "Incluimos historias en las que distintos medios usan esas palabras por separado." in html
    assert "exacta=1" in html and "Buscar solo mis palabras" in html
    assert "Incluida porque los medios usan estas palabras por separado" in html
    assert "«paga» (equivalente de «subvención») en El Español" in html
    assert "«zorrilla» en El País" in html
    assert "ayuda, bono" not in html  # only equivalents that matched something are listed


def test_exacta_disables_expansion_and_story_level(client):
    html = page(client, q="subvención zorrilla", exacta="1")
    assert "También hemos buscado" not in html and "Incluimos historias" not in html
    assert "Búsqueda literal" in html and "Incluir palabras equivalentes" in html
    assert "Una paga de pádel" not in html
    assert 'name="exacta" value="1"' in html


def test_plain_search_has_no_extra_notice(client):
    html = page(client, q="zorrilla remo")
    assert "También hemos buscado" not in html and "Incluimos historias" not in html and "no hay resultados con todas" not in html.lower()


def test_approximate_results_notice_and_suggestions(client):
    html = page(client, q="zorrilla remo quimerazzz")
    assert "No hay resultados con todas las palabras. Mostramos resultados aproximados que contienen" in html
    assert "«zorrilla»" in html and "«quimerazzz»" in html
    assert 'href="/buscar?q=zorrilla+remo' in html.replace("%20", "+") and "Prueba con" in html


def test_empty_state_when_nothing_matches(client):
    html = page(client, q="quimerazzz ballenazzz")
    assert "No hemos encontrado titulares" in html and "Prueba con menos palabras" in html
    assert "aproximados" not in html


def test_api_accepts_exacta(client):
    main._cache.clear()
    a = client.get("/api/buscar", params={"q": "subvención zorrilla"}).json()
    b = client.get("/api/buscar", params={"q": "subvención zorrilla", "exacta": "1"}).json()
    urls = lambda d: {x["url"] for st in d["stories"] for x in st}  # noqa: E731
    assert "https://elespanol.com/zz2" in urls(a) and "https://elespanol.com/zz2" not in urls(b)


# --- gold set --------------------------------------------------------------------------------------------------------
def load_script():
    spec = importlib.util.spec_from_file_location("eval_search", ROOT / "scripts" / "eval_search.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gold_set_expanded_search_finds_every_case(tmp_path):
    ev = load_script()
    cases = ev.load_cases()
    assert len(cases) >= 15
    literal = 0
    for i, case in enumerate(cases):
        db = Database(tmp_path / f"g{i}.sqlite3")
        ev.seed_case(db, case)
        strict, wide = ev.run_case(db, case)
        assert wide, f"expanded search misses {case['query']!r}"
        literal += strict
    assert literal < len(cases) // 2  # the cases are chosen for wording mismatches


def test_docs_pages_describe_and_link_the_synonym_list(client):
    for path in ("/metodologia", "/como-leer"):
        html = client.get(path).text
        assert "data/synonyms.json" in html or "synonyms.json" in html
    assert "Palabras equivalentes" in client.get("/metodologia").text
    assert "Cómo buscar" in client.get("/como-leer").text
