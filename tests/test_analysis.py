from app.analysis import (
    Story,
    blind_spots,
    cluster,
    coverage,
    coverage_summary,
    dedupe,
    make_article,
    norm_title,
    story_score,
)
from app.catalog import get_catalog

cat = get_catalog()


def art(media_id, title, published=1000, relevance=1.0):
    a = make_article(cat, title=title, url=f"https://x.test/{media_id}/{abs(hash(title))}", summary="",
                     published=published, media_id=media_id)
    a.relevance = relevance
    return a


def unrated_ids(n):
    return [m.id for m in cat.news_media if m.assessment.position is None][:n]


def ids_on(side, n):
    f = {"left": lambda p: p < 0, "right": lambda p: p > 0, "center": lambda p: p == 0}[side]
    return [m.id for m in cat.news_media if m.assessment.position is not None and f(m.assessment.position)][:n]


def test_norm_title_ignores_case_accents_spaces():
    assert norm_title("  Las  19 COINCIDENCIAS  de Él ") == "las 19 coincidencias de el"


def test_dedupe_merges_identical_titles_and_keeps_order():
    a = [art("el-pais", "Ley de vivienda aprobada"), art("abc", "Otra cosa distinta"),
         art("okdiario", "ley de  vivienda APROBADA")]
    rows = dedupe(a)
    assert [len(r.articles) for r in rows] == [2, 1]
    assert rows[0].lead.source_name == a[0].source_name


def test_row_group_only_when_shared():
    vocento = [m.id for m in cat.news_media if m.group and m.group.id == "vocento"][:2]
    assert len(vocento) == 2
    shared = dedupe([art(i, "Mismo titular") for i in vocento])[0]
    assert shared.group and shared.group.id == "vocento"
    mixed = dedupe([art(vocento[0], "Mismo titular"), art("el-pais", "Mismo titular")])[0]
    assert mixed.group is None


def test_story_rows_dedupes_but_coverage_counts_real_publications():
    ids = ids_on("right", 3)
    arts = [art(i, "Gobierno aprueba nueva ley vivienda alquiler") for i in ids]
    s = cluster(arts)[0]
    assert len(s.articles) == 3 and len(s.rows) == 1
    assert coverage(s.articles).total == 3


def test_ranking_rewards_diversity_and_sinks_syndicated_or_unlabelled():
    left, right = ids_on("left", 1)[0], ids_on("right", 1)[0]
    diverse = [art(left, "Titular uno distinto"), art(right, "Titular dos distinto")]
    same_side = [art(i, f"Titular {k} distinto") for k, i in enumerate(ids_on("right", 2))]
    syndicated = [art(left, "Mismo titular"), art(right, "Mismo titular")]
    unlabelled = [art(i, f"Titular {k} otro") for k, i in enumerate(unrated_ids(2))]
    assert story_score(diverse) > story_score(same_side)
    assert story_score(diverse) > story_score(syndicated)
    assert story_score(same_side) > story_score(unlabelled)


def test_comparison_needs_two_sides_and_picks_latest():
    lf, r = ids_on("left", 1)[0], ids_on("right", 2)
    story = Story([art(lf, "A", 10), art(r[0], "B", 10), art(r[1], "C", 99)])
    cmp = {k: a for k, _, a in story.comparison}
    assert cmp["center"] is None and cmp["left"].title == "A" and cmp["right"].title == "C"
    assert Story([art(r[0], "B"), art(r[1], "C")]).comparison is None


def test_blind_spots_threshold():
    rights = ids_on("right", 4)
    one_sided = Story([art(i, f"t{k}") for k, i in enumerate(rights)])
    mixed = Story([art(rights[0], "a"), art(rights[1], "b"), art(ids_on("left", 1)[0], "c")])
    small = Story([art(rights[0], "a"), art(rights[1], "b")])
    out = blind_spots([mixed, small, one_sided])
    assert [b["story"] for b in out] == [one_sided]
    assert out[0]["side"] == "right" and out[0]["labelled"] == 4
    assert blind_spots([mixed, small]) == []


def _cov(by, total):
    return coverage_stub(by, total)


def coverage_stub(by, total):
    from app.analysis import Coverage
    full = {-2: 0, -1: 0, 0: 0, 1: 0, 2: 0, **by}
    return Coverage(total=total, outlets=1, rated=sum(full.values()), by_position=full, by_group=[], by_media=[],
                    left_share=0, center_share=0, right_share=0, warnings=[])


def test_coverage_summary_sentences():
    text = coverage_summary(_cov({-1: 33, 1: 40, 2: 21, 0: 12}, 209))
    assert text.startswith("De los 106 titulares de medios con etiqueta, el 58 % procede de medios situados "
                           "a la derecha del centro")
    assert "el 31 % de medios situados a la izquierda" in text
    assert "Calculado sobre 106 de 209 titulares (el resto son de medios sin etiqueta publicada)." in text
    assert "Dato orientativo" not in text
    assert coverage_summary(_cov({}, 5)).startswith("Ningún titular procede")
    one = coverage_summary(_cov({1: 1}, 3))
    assert "Del único titular" in one and "Calculado sobre 1 de 3 titulares" in one
    assert "Dato orientativo: menos de la mitad de los titulares tienen etiqueta." in one
    assert "sin etiqueta" not in coverage_summary(_cov({0: 3}, 3))




def test_language_mark_and_spread_labels():
    from app.facts import language_note
    assert language_note(["catalán"]) == "en catalán"
    assert language_note(["español", "catalán"]) is None
    assert language_note(["gallego", "euskera"]) == "en gallego y euskera"
    assert language_note([]) is None and language_note(["inglés"]) is None
    cat = get_catalog()
    ca = [m for m in cat.news_media if m.derived.languages and "español" not in m.derived.languages
          and "catalán" in m.derived.languages]
    assert ca, "catalog has an outlet whose only language is Catalan"
    a = make_article(cat, title="Titular", url="https://x.test/ca", summary="", published=1, media_id=ca[0].id)
    assert a.lang_note == "en catalán"
    es = make_article(cat, title="Titular", url="https://x.test/es", summary="", published=1, media_id="el-pais")
    assert es.lang_note is None
    assert "signals" not in a.to_public()


def test_coverage_stack_has_explicit_unlabelled_segment():
    from app import main
    tpl = main.templates.get_template("_macros.html").module
    html = tpl.coverage_panel(coverage_stub({1: 2}, 6))
    assert "stack__seg--na" in html and "w-67" in html and "style=" not in html
    assert "Calculado sobre 2 de 6 titulares" in html
