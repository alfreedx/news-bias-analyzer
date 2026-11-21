"""CIS public perception: a separate kind of evidence that only positions outlets with no label."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from app import main
from app.analysis import coverage, make_article
from app.catalog import Catalog, get_catalog
from app.config import settings
from app.perception import PerceptionEntry, band, load_perception, position_of

RAW = json.loads((settings.catalog_dir / "perception.json").read_text(encoding="utf-8"))
catalog = get_catalog()
_n = [0]


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def entry(mean, name="X", kind="periódico", question=22):
    return PerceptionEntry(name, kind, question, mean, 2.0, 1000)


def art(media_id, title="Titular"):
    _n[0] += 1
    return make_article(catalog, title=title, url=f"https://x.test/{media_id}/{_n[0]}", summary="",
                        published=1000, media_id=media_id)


def panel(c):
    return main.templates.get_template("_macros.html").module.coverage_panel(c)


def test_meta_and_questions_are_verbatim():
    meta = RAW["_meta"]
    assert meta["study_number"] == 3511 and meta["sample"] == 15017 and meta["fieldwork"] == "mayo de 2025"
    assert meta["url"].startswith("https://www.cis.es/") and meta["retrieved"] == "2026-11-21"
    assert set(meta["questions"]) == {"22", "23", "24"}
    assert meta["questions"]["24"] == "¿Y dónde colocaría las siguientes emisoras de radio?"
    assert "Carlos Gonzalo" in meta["acknowledgement"]
    assert meta["scale"] == "1 = lo más a la izquierda, 10 = lo más a la derecha"


def test_every_entry_is_known_and_valid():
    data = load_perception(settings.catalog_dir, set(catalog.media))
    assert [e.cis_name for e in data.media["rtve"]] == ["TVE1", "Canal 24 horas", "RNE"]
    assert data.media["cadena-ser"][0].mean == 4.02 and data.media["cadena-ser"][0].n == 11927
    assert "el-confidencial-digital" not in data.media and "libertad-digital" not in data.media
    assert all(1 <= e.mean <= 10 for es in data.media.values() for e in es)


def _dir_with(tmp_path, media):
    shutil.copy(settings.catalog_dir / "perception.json", tmp_path / "perception.json")
    raw = json.loads((tmp_path / "perception.json").read_text(encoding="utf-8"))
    raw["media"] = media
    (tmp_path / "perception.json").write_text(json.dumps(raw), encoding="utf-8")
    return tmp_path


GOOD = [{"cis_name": "Cadena SER", "kind": "emisora de radio", "question": 24, "mean": 4.02, "sd": 2.43, "n": 11927}]


def test_loader_rejects_unknown_ids(tmp_path):
    with pytest.raises(ValueError, match="unknown outlets"):
        load_perception(_dir_with(tmp_path, {"no-existe": GOOD}), set(catalog.media))


@pytest.mark.parametrize("mean", [0.9, 10.1])
def test_loader_rejects_means_outside_scale(tmp_path, mean):
    bad = [{**GOOD[0], "mean": mean}]
    with pytest.raises(ValueError, match="outside 1-10"):
        load_perception(_dir_with(tmp_path, {"cadena-ser": bad}), set(catalog.media))


def test_loader_rejects_kind_question_mismatch(tmp_path):
    bad = [{**GOOD[0], "question": 22}]
    with pytest.raises(ValueError, match="does not match"):
        load_perception(_dir_with(tmp_path, {"cadena-ser": bad}), set(catalog.media))


def test_catalog_fails_fast_on_unknown_id(tmp_path):
    for name in ("groups.json", "media.json"):
        shutil.copy(settings.catalog_dir / name, tmp_path / name)
    _dir_with(tmp_path, {"no-existe": GOOD})
    with pytest.raises(ValueError):
        Catalog(tmp_path)


@pytest.mark.parametrize("mean,pos", [
    (1.0, -2), (2.8, -2), (2.81, -1), (4.6, -1), (4.61, 0), (6.4, 0), (6.41, 1), (8.2, 1), (8.21, 2), (10.0, 2),
])
def test_band_edges(mean, pos):
    assert band(mean) == pos


def test_rtve_rule():
    assert position_of([entry(3.96), entry(4.13), entry(4.29)]) == -1
    assert position_of([entry(3.96), entry(6.0), entry(4.29)]) is None


def test_labelled_outlet_keeps_label_position_and_shows_cis(client):
    a = catalog.media["el-pais"].assessment
    assert a.perception and not a.by_perception and a.position == -1 and a.evidence
    page = client.get("/medios/el-pais").text
    assert "Cómo lo percibe el público" in page and "Qué dicen las fuentes" in page
    assert "quienes conocen El País lo sitúan de media en <strong" in page and ">4,2<" in page
    assert "11.817 respuestas; desviación típica 2,5" in page
    assert "Ver la tabla del CIS (preguntas 22–24)" in page
    assert "Es lo que opina la población encuestada, no un análisis de lo que publica el medio." in page
    verdict = page.split("Orientación ideológica")[1].split("Cómo lo percibe")[0]
    assert "según la percepción del público" not in verdict


def test_unlabelled_outlet_is_classified_by_perception(client):
    a = catalog.media["cadena-ser"].assessment
    assert not a.evidence and a.by_perception and a.position == -1 and a.label == "Centroizquierda"
    page = client.get("/medios/cadena-ser").text
    assert "según la percepción del público (CIS 2025), no por etiquetas publicadas" in page
    assert "Posición según la percepción del público (CIS)" in page and "chip--perc" in page
    assert "quienes conocen Cadena SER la sitúan de media en" in page
    assert "style=" not in page
    api = {x["id"]: x for x in client.get("/api/medios").json()["media"]}["cadena-ser"]
    assert api["ideology"]["basis"] == "perception" and api["ideology"]["score"] is None
    assert api["perception"][0]["mean"] == 4.02
    assert "(CIS)" in client.get("/medios").text


def test_rtve_shows_three_media_and_gets_position(client):
    a = catalog.media["rtve"].assessment
    assert a.by_perception and a.position == -1
    page = client.get("/medios/rtve").text
    assert "Medido sobre TVE1, Canal 24 horas y RNE" in page
    assert page.count('class="perception__item"') == 3


def test_outlet_without_cis_has_no_block(client):
    assert "Cómo lo percibe el público" not in client.get("/medios/deia").text


def test_perception_never_enters_the_label_mean():
    for m in catalog.news_media:
        a = m.assessment
        if a.evidence and a.perception:
            assert not a.by_perception
        if a.by_perception:
            assert not [e for e in a.evidence if e.position is not None]


def test_coverage_counts_perception_outlets_as_labelled():
    c = coverage([art("cadena-ser"), art("el-pais"), art("deia")])
    assert c.rated == 2 and c.perception_outlets == 1
    assert "1 medio está clasificado por la percepción del público (CIS), no por etiquetas." in panel(c)
    c2 = coverage([art("cadena-ser"), art("antena3"), art("el-pais")])
    assert "2 medios están clasificados por la percepción del público (CIS), no por etiquetas." in panel(c2)
    assert "data-perception-note" not in panel(coverage([art("el-pais")]))


def test_methodology_has_table_questions_and_sources(client):
    page = client.get("/metodologia").text
    for text in ("Percepción del público (CIS)", "de 1 a 2,8 (ambos incluidos)", "más de 2,8 y hasta 4,6",
                 "más de 4,6 y hasta 6,4", "más de 6,4 y hasta 8,2", "más de 8,2 y hasta 10",
                 "Estudio nº 3511", "medido sobre TVE1, Canal 24 horas y RNE", "Fuentes valoradas y no usadas",
                 "doi:10.15581/003.35.1.29-43", "Media Bias Chart España", "Periódicos por ideología",
                 "¿Y dónde colocaría las siguientes emisoras de radio?"):
        assert text in page, text
    assert "Cadena SER" in page and "TV3" in page and "data-cis-bands" in page
