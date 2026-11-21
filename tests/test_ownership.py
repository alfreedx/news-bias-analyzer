"""data/ownership.json: every row is a citation, and the pages show it (or say nothing is known)."""

import json
import re
from datetime import date
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import main
from app.catalog import NO_OWNERSHIP_DATA, get_catalog
from app.config import settings

RAW = json.loads((settings.catalog_dir / "ownership.json").read_text(encoding="utf-8"))
catalog = get_catalog()
ENTRIES = [("groups", k, v) for k, v in RAW["groups"].items()] + [("outlets", k, v) for k, v in RAW["outlets"].items()]
PRIVATE_NAMES = ["Yolanda González", "Jesús Maraña", "Ortiz de Elguea", "Manuel Rico", "Crespo Lorenzo",
                 "Assumpció Maresma", "Vicent Partal"]


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_meta():
    meta = RAW["_meta"]
    assert meta["license"] == "CC BY-SA 4.0" and meta["description"] and meta["retrieved"]


@pytest.mark.parametrize("section,key,entry", ENTRIES, ids=lambda v: v if isinstance(v, str) else "")
def test_rows_are_citations(section, key, entry):
    assert entry["rows"], f"{section}/{key} has no rows"
    for row in entry["rows"]:
        for field in ("text", "source_title", "url", "date", "date_kind", "kind"):
            assert isinstance(row.get(field), str) and row[field].strip(), f"{key}: empty {field}"
        assert urlsplit(row["url"]).scheme == "https"
        date.fromisoformat(row["date"])
        assert row["date_kind"] in {"datos", "consulta"}
        assert row["kind"] in {"accionista", "propia", "financiacion", "publico"}
        if row["kind"] in {"propia", "financiacion"}:
            assert row["text"].startswith("Según el propio medio")


def test_keys_exist_in_catalog():
    assert set(RAW["groups"]) <= set(catalog.groups)
    assert set(RAW["outlets"]) <= set(catalog.media)


def test_no_forbidden_content():
    blob = json.dumps(RAW, ensure_ascii=False).lower()
    assert "interes" not in blob and "intereses" not in blob
    for name in PRIVATE_NAMES:
        assert name.lower() not in blob


def test_shareholders_sorted_desc():
    for key in ("prisa", "vocento"):
        pcts = [float(re.search(r": ([\d,\.]+) %", r["text"]).group(1).replace(".", "").replace(",", "."))
                for r in RAW["groups"][key]["rows"]]
        assert pcts == sorted(pcts, reverse=True)


def test_outlet_with_data(client):
    html = client.get("/medios/el-pais").text
    assert "Quién figura como propietario" in html
    assert "Oviedo Holdings" in html and "participación indirecta" in html
    assert "datos a 22/05/2023" in html
    assert "cnmv.es" in html
    assert "¿Ves un dato incorrecto? Avísanos" in html


def test_outlet_without_data(client):
    html = client.get("/medios/20minutos").text
    assert NO_OWNERSHIP_DATA in html


def test_note_present(client):
    for path in ("/medios/el-pais", "/medios/20minutos", "/grupos/prisa"):
        html = client.get(path).text
        assert "Qué no significa esto" in html
        assert "no demuestra que influya" in html and "no deben sumarse" in html


def test_group_page(client):
    html = client.get("/grupos/vocento").text
    assert "Quién figura como propietario" in html and "Valjarafe" in html
    assert "consultado el" in html or "datos a" in html


def test_rtve_wording(client):
    owners = catalog.groups["rtve"].owners
    assert "SEPI" not in owners and "íntegramente estatal" in owners
    html = client.get("/grupos/rtve").text
    assert "participada por la SEPI" not in html
    assert "boe.es" in html


def test_eldiario_group_no_wikipedia_percentage():
    assert "70" not in catalog.groups["diario-prensa-digital"].owners


def test_natural_persons_aggregated(client):
    html = client.get("/medios/infolibre").text
    assert "cuatro personas físicas con un 4,48 % cada una" in html
    for name in PRIVATE_NAMES:
        assert name not in html


def test_publico_names_beneficial_owner_only(client):
    html = client.get("/medios/publico").text
    assert "Jaume Roures" in html and "Crespo" not in html


def test_api_exposes_ownership(client):
    data = client.get("/api/medios").json()
    by_id = {m["id"]: m for m in data["media"]}
    assert by_id["el-pais"]["ownership_facts"]["rows"][0]["url"].startswith("https://")
    assert by_id["20minutos"]["ownership_facts"]["if_empty"] == NO_OWNERSHIP_DATA
    assert all("ownership_facts" in g for g in data["groups"])
