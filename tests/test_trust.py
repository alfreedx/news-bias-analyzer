"""Trust and clarity rules: honest percentages, section evidence, relevance, fact-check cues, story links."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.analysis import Coverage, Story, coverage, coverage_summary, make_article
from app.catalog import get_catalog
from app.consensus import build_voices, consensus
from app.factcheck import verdict_cue
from app.sections import incident_evidence, opinion_evidence, query_is_incident
from app.stories import link_clusters

cat = get_catalog()
_n = [0]


def art(media_id="abc", title="Titular", url=None, summary="", categories=None, published=1000, article_id=None):
    _n[0] += 1
    return make_article(cat, title=title, url=url or f"https://x.test/{media_id}/{_n[0]}", summary=summary,
                        published=published, media_id=media_id, categories=categories, article_id=article_id)


def cov(by, total):
    full = {-2: 0, -1: 0, 0: 0, 1: 0, 2: 0, **by}
    return Coverage(total=total, outlets=1, rated=sum(full.values()), by_position=full, by_group=[], by_media=[],
                    left_share=0, center_share=0, right_share=0, warnings=[])


def macros():
    return main.templates.get_template("_macros.html").module


# --- B1: persisted story ids ------------------------------------------------------------
def test_two_clusters_never_share_a_persisted_story():
    """Seen live: several different front-page stories about one election campaign all linked to /historia/1."""
    def cluster_of(ids):
        return [art(title=f"t{i}", article_id=i) for i in ids]

    a, b, c = cluster_of([1, 2, 3]), cluster_of([4, 5, 6]), cluster_of([7, 8, 9, 10])
    story_of = dict.fromkeys(range(1, 11), 1)  # one persisted story swallowed all of them
    ids = link_clusters([a, b, c], story_of)
    assert ids.count(1) == 1 and ids.count(None) == 2
    assert ids[2] == 1  # the cluster with most headlines in the persisted story keeps it
    # distinct persisted stories: each cluster keeps its own
    assert link_clusters([a, b], {1: 5, 2: 5, 3: 5, 4: 6, 5: 6, 6: 6}) == [5, 6]
    # the majority rule still applies
    assert link_clusters([a], {1: 5}) == [None]


def test_tie_goes_to_the_better_ranked_cluster():
    a = [art(article_id=1), art(article_id=2)]
    b = [art(article_id=3), art(article_id=4)]
    assert link_clusters([a, b], {1: 9, 2: 9, 3: 9, 4: 9}) == [9, None]


def test_link_stories_assigns_unique_ids_on_the_front_page(monkeypatch):
    clusters = [Story([art(article_id=i) for i in ids]) for ids in ([1, 2, 3], [4, 5, 6])]
    monkeypatch.setattr(main.db, "story_ids_for", lambda ids: dict.fromkeys(ids, 1))
    main.link_stories(clusters)
    assert [s.story_id for s in clusters].count(1) == 1


# --- T1: honest percentages -----------------------------------------------------------
def test_percentages_only_with_30_labelled_and_half_of_the_total():
    solid = cov({-1: 20, 1: 25}, 60)
    assert solid.reliable
    assert "%" in coverage_summary(solid)
    for weak in (cov({-1: 10, 1: 10}, 25),   # fewer than 30 labelled
                 cov({-1: 20, 1: 20}, 100)):  # 40 labelled but under half of the total
        assert not weak.reliable
        text = coverage_summary(weak)
        assert "%" not in text
        assert text.startswith(f"Poca base para comparar: solo {weak.rated} de {weak.total} titulares proceden de "
                               "medios con etiqueta publicada.")
        assert "refleja también cuánto publica cada medio" in text


def test_thin_base_without_unlabelled_headlines_reads_naturally():
    text = coverage_summary(cov({1: 12}, 12))
    assert text.startswith("Poca base para comparar: solo hay 12 titulares, todos de medios con etiqueta publicada.")
    assert "solo 12 de 12" not in text


def test_summary_always_says_the_split_reflects_publishing_volume():
    assert "cuánto publica cada medio" in coverage_summary(cov({1: 40}, 50))
    assert "cuánto publica cada medio" in coverage_summary(cov({}, 5))


def test_panel_hides_bar_and_percentages_without_a_base():
    weak = macros().coverage_panel(cov({1: 2}, 6))
    assert "stack__seg" not in weak and 'class="stack"' not in weak and "%" not in weak
    assert "Poca base para comparar" in weak
    strong = macros().coverage_panel(cov({1: 35}, 40))
    assert 'class="stack"' in strong and "stack__seg--r1" in strong and "style=" not in strong


# --- T9: agency note -------------------------------------------------------------------
def test_agency_note_counts_headlines_with_an_agency_mark():
    arts = [art("abc", "Cifra uno (EFE)"), art("publico", "Cifra dos (EFE)"), art("el-pais", "Otra (Europa Press)"),
            art("okdiario", "Sin marca")]
    c = coverage(arts)
    assert c.agency_headlines == 3 and c.agency_names == ["EFE", "Europa Press"]
    assert "3 titulares reproducen textos de agencia (EFE, Europa Press)" in macros().coverage_panel(c)
    assert "data-agency" not in macros().coverage_panel(coverage([art("abc", "Sin marca")]))


# --- T4: opinion -----------------------------------------------------------------------
@pytest.mark.parametrize("url,cats,expected", [
    ("https://elpais.com/opinion/2026-11-18/titulo.html", None, "Ruta /opinion/"),
    ("https://x.es/a/b.html", "Política | Opinión", "Categoría RSS «Opinión»"),
    ("https://x.es/tribuna/algo.html", None, "Ruta /tribuna/"),
    ("https://x.es/analisis/algo.html", None, None),               # «análisis» is not opinion
    ("https://x.es/a/opinion.html", None, None),                   # the slug itself is ignored
    ("https://x.es/cartas-al-director/x.html", None, "Ruta /cartas-al-director/"),
])
def test_opinion_evidence(url, cats, expected):
    assert opinion_evidence(url, cats) == expected


def test_opinion_pieces_are_not_consensus_voices_but_stay_in_coverage():
    news = [art("el-pais", "El Gobierno aprueba la ley de vivienda"), art("abc", "Gobierno aprueba la ley de vivienda")]
    op = art("okdiario", "Ley de vivienda: una columna", url="https://okdiario.com/opinion/ley.html")
    assert op.opinion and not news[0].opinion
    assert len(build_voices([*news, op])) == 2
    assert consensus([*news, op]).voices == 2
    assert coverage([*news, op]).total == 3


# --- T2: relevance --------------------------------------------------------------------
def test_incident_evidence_and_query_words():
    assert incident_evidence("https://x.es/sucesos/incendio.html", None) == "Ruta /sucesos/"
    assert incident_evidence("https://x.es/a.html", "Sucesos y tribunales") == "Categoría RSS «Sucesos y tribunales»"
    assert incident_evidence("https://x.es/economia/vivienda.html", "Economía | Vivienda") is None
    assert not query_is_incident("vivienda")
    assert query_is_incident("incendio vivienda") and query_is_incident("detenidos")


def test_incidental_story_needs_three_outlets_to_reach_the_top():
    policy = Story([art("el-pais", "Plan de vivienda"), art("abc", "Plan de vivienda pública")])
    fire = [art(m, "Incendio en una vivienda", url=f"https://x.es/{m}/sucesos/i.html") for m in ("el-pais", "abc")]
    s_fire = Story(fire)
    s_fire.incidental = True
    top, rest = main.split_top([s_fire, policy])
    assert top == [policy] and rest == [s_fire]
    big = Story([*fire, art("publico", "Incendio en una vivienda", url="https://x.es/p/sucesos/i.html")])
    big.incidental = True
    assert big in main.split_top([big, policy])[0]
    single = Story([art("abc", "Suelto")])
    top, rest = main.split_top([single, policy])
    assert top == [policy] and rest == [single]


def test_top_stories_are_limited_to_five():
    stories = [Story([art("el-pais", f"Tema {i}"), art("abc", f"Tema {i} otra vez")]) for i in range(8)]
    top, rest = main.split_top(stories)
    assert len(top) == 5 and len(rest) == 3


@pytest.fixture(scope="module")
def client():
    now = int(time.time())
    rows = []
    for i, m in enumerate(("el-pais", "abc", "eldiario-es")):
        rows.append((f"https://x{i}.test/economia/p{i}.html", m,
                     f"El Gobierno presenta el plan de vivienda tarpeya {'para alquiler asequible' if i % 2 else 'de alquiler asequible'}",
                     "Medidas de vivienda tarpeya.", now - 3600 * 5 - i, None, "Economía"))
        rows.append((f"https://x{i}.test/sucesos/f{i}.html", m,
                     "Incendio en una vivienda tarpeya de Valencia deja dos heridos",
                     "Los bomberos sofocaron el fuego.", now - 60 - i, None, "Sucesos"))
    rows.append(("https://pais.test/opinion/o.html", "el-pais", "Vivienda tarpeya: una columna sobre el plan", "x",
                 now - 100, None, "Opinión"))
    main.db.insert_articles(rows)
    from app.stories import assign_stories
    assign_stories(main.db, main.catalog)
    with TestClient(main.app) as c:
        yield c


def test_search_puts_topical_stories_first_and_folds_the_rest(client):
    r = asyncio.run(main.run_search("vivienda tarpeya", 30))
    assert r["top"], "the policy story reaches the top"
    assert "plan de vivienda" in r["top"][0]["story"].lead.title  # not the opinion column
    # the fire story is carried by 3 outlets here, so it may qualify; with fewer it must stay in the fold
    for info in r["top"]:
        if info["story"].incidental:
            assert info["story"].outlets >= 3
    html = client.get("/buscar", params={"q": "vivienda tarpeya"}).text
    assert "data-brief" in html and "En resumen:" in html and "Verificaciones:" in html
    assert "Ningún verificador" in html


def test_incident_story_with_two_outlets_goes_to_the_fold(client):
    main.db.insert_articles([
        ("https://a.test/sucesos/z1.html", "el-pais", "Incendio en una vivienda zarzamora: dos heridos", "", int(time.time()), None, "Sucesos"),
        ("https://b.test/sucesos/z2.html", "abc", "Un incendio en una vivienda zarzamora deja dos heridos", "", int(time.time()), None, "Sucesos"),
        ("https://c.test/economia/z3.html", "publico", "Plan de vivienda zarzamora para jóvenes", "", int(time.time()) - 7200, None, "Economía"),
        ("https://d.test/economia/z4.html", "eldiario-es", "El plan de vivienda zarzamora para los jóvenes", "", int(time.time()) - 7300, None, "Economía"),
    ])
    r = asyncio.run(main.run_search("vivienda zarzamora", 30))
    assert [i["story"].lead.title for i in r["top"]] == [r["top"][0]["story"].lead.title]
    assert "Plan de vivienda" in r["top"][0]["story"].lead.title
    assert any("Incendio" in s.lead.title for s in r["rest"])
    html = client.get("/buscar", params={"q": "vivienda zarzamora"}).text
    assert html.index("Plan de vivienda zarzamora") < html.index("Más titulares (") < html.index("Incendio en una vivienda")


def test_incident_query_is_not_demoted(client):
    r = asyncio.run(main.run_search("incendio vivienda tarpeya", 30))
    assert r["top"] and "Incendio" in r["top"][0]["story"].lead.title


def test_incident_items_rank_after_topical_ones(client):
    arts = asyncio.run(main.run_search("vivienda tarpeya", 30))["articles"]
    assert arts[0].incident_evidence is None
    first_incident = next(i for i, a in enumerate(arts) if a.incident_evidence)
    assert all(not a.incident_evidence for a in arts[:first_incident])


def test_opinion_badge_has_evidence_in_title(client):
    html = client.get("/buscar", params={"q": "vivienda tarpeya"}).text
    assert 'class="mark-opinion"' in html and "Categoría RSS «Opinión»" in html


def test_no_match_message_is_not_in_the_html(client):
    assert "Ningún titular coincide" not in client.get("/buscar", params={"q": "vivienda tarpeya"}).text
    assert "data-no-match" not in client.get("/buscar", params={"q": "vivienda tarpeya"}).text


# --- T3: fact-check verdict cues --------------------------------------------------------
@pytest.mark.parametrize("title,cue", [
    ("Es falso que el Gobierno vaya a prohibir las gasolineras", "falso"),
    ("Bulo: no hay una alerta sanitaria en Madrid", "bulo"),
    ("Engañoso: el vídeo no es de esta semana", "engañoso"),
    ("No, la Seguridad Social no cobra por jubilarse", "no"),
    ("Sí, es cierto que el IVA baja en alimentos", "cierto"),
    ("Imagen descontextualizada de las inundaciones", "descontextualizado"),
    ("Qué sabemos de la reforma de la vivienda", None),
    ("Cómo se combate y se difunde en las redes sociales durante campañas el bulo", None),  # cue not at the start
    ("Novedades sobre el alquiler", None),
    ("", None),
])
def test_verdict_cue(title, cue):
    assert verdict_cue(title) == cue


def test_factcheck_label_is_attributed_to_the_checker():
    a = make_article(cat, title="Es falso que se vaya a cobrar por respirar", url="https://maldita.es/1", summary="",
                     published=int(time.time()), media_id="maldita")
    html = str(macros().check_line(a))
    assert f"{a.source_name} lo califica:" in html and "«Es falso que se vaya a cobrar por respirar»" in html
    neutral = make_article(cat, title="Qué hay detrás del mensaje viral", url="https://maldita.es/2", summary="",
                           published=1, media_id="maldita")
    out = str(macros().check_line(neutral))
    assert "lo califica" not in out and f"Verificación de {neutral.source_name}:" in out


# --- T5, T6, T7, T8 --------------------------------------------------------------------
def story_id_with(title_part):
    with main.db.connect() as c:
        return c.execute("SELECT id FROM stories WHERE title LIKE ? ORDER BY id DESC", (f"%{title_part}%",)).fetchone()[0]


def test_story_page_shows_majority_headline_attributed(client):
    html = client.get(f"/historia/{story_id_with('plan de vivienda tarpeya')}").text
    assert "La noticia, según la mayoría" in html and "Según " in html
    assert "Titular más representativo (el que más comparte con los demás)" in html
    assert html.index("La noticia, según la mayoría") < html.index("En resumen")
    assert "Noticia muy reciente" not in html  # first headline is 5 h old


def test_recent_story_is_flagged(client):
    html = client.get(f"/historia/{story_id_with('Incendio en una vivienda tarpeya')}").text
    assert "Noticia muy reciente: la cobertura todavía está creciendo." in html


def test_missing_side_wording():
    arts = [art("eldiario-es", "A"), art("abc", "B")]
    html = macros().comparison(Story(arts, seed=arts[0]))
    assert "No aparece en los RSS que leemos de medios de este lado" in html
    assert "No hemos encontrado" not in html


def test_home_positioning_sentence(client):
    assert ("Contraste no decide qué es verdad: te enseña quién cuenta cada noticia, cómo la titula cada lado y "
            "qué han comprobado los verificadores.") in client.get("/").text
