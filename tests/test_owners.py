"""Owner descriptions: loader, formatting, rendering, API and the exclusions that protect personal data."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import enrich_owners as eo  # noqa: E402

from app import main, owners  # noqa: E402
from app.config import settings  # noqa: E402

PERSON = {
    "label": "Ana Prueba", "description": "empresaria española", "description_lang": "es", "kind": "person",
    "url": "https://www.wikidata.org/wiki/Q900", "retrieved": "2026-11-21",
    "occupations": ["empresaria", "gestora"],
    "positions": [{"label": "presidente de Acme", "start": 2014, "end": None},
                  {"label": "ministra de Hacienda", "start": 2010, "end": 2016}],
    "employers": [{"label": "Banco X", "start": 2001, "end": 2009}], "country": "España",
}
ORG = {
    "label": "Fondo Z", "description": "fondo de inversión", "description_lang": "es", "kind": "organisation",
    "url": "https://www.wikidata.org/wiki/Q901", "retrieved": "2026-11-21", "instance_of": ["fondo de inversión"],
    "industry": ["finanzas"], "country": "Reino Unido", "hq": "Londres",
    "parent": [{"qid": "Q902", "label": "Holding Y"}], "inception": 2005,
}
BARE = {"label": "Sin Texto", "description": None, "kind": "organisation",
        "url": "https://www.wikidata.org/wiki/Q903", "retrieved": "2026-11-21"}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(owners, "load_owners", lambda: {"Q900": PERSON, "Q901": ORG, "Q903": BARE})


def facts_of(view):
    return {f["label"]: f["value"] for f in view["facts"]}


def test_loader_is_fail_safe_when_file_missing(tmp_path, monkeypatch):
    assert owners.load_owners(tmp_path) == {}
    (tmp_path / "owners.json").write_text("not json", encoding="utf-8")
    owners._load.cache_clear()
    assert owners.load_owners(tmp_path) == {}
    owners._load.cache_clear()
    monkeypatch.setattr(owners, "load_owners", lambda: {})
    assert owners.owner_view("Q1") is None


def test_loader_reads_file(tmp_path):
    (tmp_path / "owners.json").write_text(json.dumps({"owners": {"Q900": PERSON}}), encoding="utf-8")
    assert owners.load_owners(tmp_path)["Q900"]["label"] == "Ana Prueba"
    owners._load.cache_clear()


@pytest.mark.parametrize("start,end,text", [(2014, None, "desde 2014"), (2010, 2016, "2010–2016"),
                                            (None, 2016, "hasta 2016"), (2012, 2012, "2012"), (None, None, "")])
def test_years(start, end, text):
    assert owners.years(start, end) == text


def test_person_view_current_and_past_positions(fake):
    v = owners.owner_view("Q900")
    f = facts_of(v)
    assert f["Ocupación"] == "empresaria, gestora"
    assert f["Cargos"] == "presidente de Acme (desde 2014), ministra de Hacienda (2010–2016)"
    assert f["Trabajo"] == "Banco X (2001–2009)" and f["Ciudadanía"] == "España"
    assert v["source"] == "https://www.wikidata.org/wiki/Q900" and v["date_label"] == "21/11/2026"


def test_organisation_view(fake):
    f = facts_of(owners.owner_view("Q901"))
    assert f == {"Tipo": "fondo de inversión", "Sector": "finanzas", "País": "Reino Unido", "Sede": "Londres",
                 "Matriz o propietario": "Holding Y", "Fundación": "2005"}


def test_missing_description_and_unknown_qid(fake):
    v = owners.owner_view("Q903")
    assert v["description"] is None and v["facts"] == []
    assert owners.owner_view("Q404") is None and owners.owner_view(None) is None


def test_views_for_facts_dedupe(fake):
    facts = {"owners_qids": ["Q900"], "owner_parents": [{"qid": "Q901", "owned_by_qids": ["Q900", "Q404"]}]}
    assert [v["qid"] for v in owners.owner_views_for_facts(facts)] == ["Q900", "Q901"]


# --- script parsing: what is never stored ---------------------------------------------
def claim(value, qualifiers=None):
    c = {"rank": "normal", "mainsnak": {"snaktype": "value", "datavalue": {"value": value}}}
    if qualifiers:
        c["qualifiers"] = qualifiers
    return c


def item(q):
    return {"entity-type": "item", "id": q}


def when(y):
    return {"snaktype": "value", "datavalue": {"value": {"time": f"+{y}-01-01T00:00:00Z", "precision": 9}}}


def test_build_owner_person_excludes_personal_and_party_data():
    body = {"snaktype": "value", "datavalue": {"value": item("Q4")}}
    ent = {
        "labels": {"es": {"value": "Ana Prueba"}}, "descriptions": {"en": {"value": "businesswoman"}},
        "claims": {
            "P31": [claim(item("Q5"))], "P106": [claim(item("Q1"))],
            "P39": [claim(item("Q2"), {"P580": [when(2014)], "P2389": [body]}),
                    claim(item("Q3"), {"P580": [when(2000)]}), claim(item("Q9"))],
            "P102": [claim(item("Q7"))], "P569": [claim({"time": "+1960-01-01T00:00:00Z", "precision": 11})],
            "P22": [claim(item("Q8"))], "P26": [claim(item("Q8"))], "P27": [claim(item("Q29"))],
        },
    }
    labels = {"Q1": "empresaria", "Q2": "presidente", "Q3": "secretario general del Partido Ejemplo", "Q4": "Acme",
              "Q7": "Partido Ejemplo", "Q8": "Familiar", "Q29": "España"}
    e = eo.build_owner("Q900", ent, labels, "2026-11-21")
    assert e["kind"] == "person" and e["description"] == "businesswoman" and e["description_lang"] == "en"
    # the party office and the unlabelled position are skipped
    assert e["positions"] == [{"label": "presidente de Acme", "start": 2014, "end": None}]
    blob = json.dumps(e)
    for forbidden in ("Partido", "1960", "Familiar", "P102", "P569", "birth"):
        assert forbidden not in blob
    assert set(e) == {"label", "description", "description_lang", "kind", "url", "retrieved", "occupations",
                      "positions", "employers", "country"}


def test_build_owner_organisation_and_no_label():
    ent = {"labels": {"en": {"value": "Fund"}}, "claims": {
        "P31": [claim(item("Q10")), claim(item("Q11"))], "P749": [claim(item("Q12"))],
        "P571": [claim({"time": "+2005-00-00T00:00:00Z", "precision": 9})]}}
    labels = {"Q10": "fondo", "Q11": "Wikidata item that needs to be split", "Q12": "Matriz"}
    e = eo.build_owner("Q901", ent, labels, "d")
    assert e["kind"] == "organisation" and e["instance_of"] == ["fondo"] and e["inception"] == 2005
    assert e["parent"] == [{"qid": "Q12", "label": "Matriz"}]
    assert eo.build_owner("Q1", {"labels": {}}, {}, "d") is None


# --- the real data -----------------------------------------------------------------------
RAW = json.loads((settings.catalog_dir / "owners.json").read_text(encoding="utf-8"))
OWNERSHIP = json.loads((settings.catalog_dir / "ownership.json").read_text(encoding="utf-8"))


def test_real_file_shape_and_exclusions():
    assert RAW["_meta"]["license"] == "CC0" and RAW["_meta"]["source"] == "Wikidata"
    assert RAW["owners"]
    for qid, o in RAW["owners"].items():
        assert o["url"] == f"https://www.wikidata.org/wiki/{qid}" and o["retrieved"] and o["label"]
        assert o["kind"] in ("person", "organisation")
        assert not {"birth", "party", "spouse", "children", "address", "net_worth"} & set(o)
        for p in o.get("positions", []):
            assert not eo.PARTY_WORDS.search(p["label"])


def test_every_ownership_qid_is_described():
    for section in ("groups", "outlets"):
        for entry in OWNERSHIP[section].values():
            for row in entry["rows"]:
                if row.get("qid"):
                    assert row["qid"] in RAW["owners"], row["text"]


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_outlet_page_shows_wikidata_owner_description(client):
    html = client.get("/medios/el-pais").text
    assert "Wikidata lo describe como" in html and "consultado el" in html
    assert "<details" in html and "Qué no significa esto" in html


def test_group_page_shareholder_with_and_without_qid(client):
    html = client.get("/grupos/prisa").text
    assert "Quién es: Joseph Oughourlian" in html  # mapped row
    assert "Oviedo Holdings S.A.R.L." in html and "Quién es: Oviedo" not in html  # unmapped: name only


def test_api_exposes_owner_descriptions(client):
    data = client.get("/api/medios").json()
    by_id = {m["id"]: m for m in data["media"]}
    assert any(o["description"] for o in by_id["el-pais"]["owners"])
    rows = {g["id"]: g for g in data["groups"]}["prisa"]["ownership_facts"]["rows"]
    mapped = [r for r in rows if r["owner"]]
    assert mapped and mapped[0]["owner"]["source"].startswith("https://www.wikidata.org/wiki/Q")
    assert any(r["owner"] is None for r in rows)
