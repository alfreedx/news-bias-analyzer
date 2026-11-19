import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import enrich_media as em  # noqa: E402

from app.catalog import get_catalog  # noqa: E402
from app.facts import NO_GROUP_TEXT, derive  # noqa: E402


def claim(value, rank="normal", qualifiers=None):
    c = {"rank": rank, "mainsnak": {"snaktype": "value", "datavalue": {"value": value}}}
    if qualifiers:
        c["qualifiers"] = qualifiers
    return c


def item(qid):
    return {"entity-type": "item", "id": qid}


def test_current_claims_skip_deprecated_and_ended():
    ent = {"claims": {"P127": [claim(item("Q1")), claim(item("Q2"), rank="deprecated"),
                               claim(item("Q3"), qualifiers={"P582": [{}]})]}}
    assert em.item_ids(ent, "P127") == ["Q1"]


def test_build_facts_only_wikidata_values():
    ent = {"claims": {
        "P407": [claim(item("Q7026"))], "P31": [claim(item("Q11032"))],
        "P127": [claim(item("Q9"))], "P159": [claim(item("Q1492"))],
        "P571": [claim({"time": "+1976-00-00T00:00:00Z", "precision": 9})],
    }}
    labels = {"Q7026": "catalán", "Q11032": "periódico", "Q9": "Grupo X", "Q1492": "Barcelona"}
    f = em.build_facts("Q5", ent, labels, "2026-11-19")
    assert f["languages"] == ["catalán"] and f["kinds"] == ["periódico"] and f["owners"] == ["Grupo X"]
    assert f["hq"] == "Barcelona" and f["founded"] == 1976 and f["source"].endswith("/Q5")
    assert "publisher" not in f
    assert "languages" not in em.build_facts("Q5", {"claims": {}}, {}, "2026-11-19")


def test_website_sanity_is_exact_host():
    ent = {"claims": {"P856": [claim("https://www.efe.com/")]}}
    assert em.website_matches(ent, ["efe.com"]) is True
    assert em.website_matches(ent, ["verifica.efe.com"]) is False
    assert em.website_matches({"claims": {}}, ["efe.com"]) is None


def _media(group=None, mtype="Diario", publisher=None, facts=None):
    return SimpleNamespace(group=group, type=mtype, publisher=publisher, owner_source="https://x.test/src",
                           facts=facts or {})


def _group(kind, name="G", gid="g"):
    return SimpleNamespace(kind=kind, name=name, id=gid)


def test_rule_public_from_wikidata_owner_or_kind():
    d = derive(_media(facts={"owners": ["Gobierno de España"], "owners_qids": ["Q1194749"], "source": "u"}))
    assert d.ownership == "publica" and d.public_text == "Gobierno de España" and d.public_via == "wikidata"
    d = derive(_media(facts={"kinds_qids": ["Q270791"], "kinds": ["empresa estatal"], "source": "u"}))
    assert d.ownership == "publica"


def test_rule_public_from_catalog_group_or_cited_publisher():
    assert derive(_media(group=_group("Medio público estatal"))).public_via == "grupo"
    d = derive(_media(mtype="Televisión y radio públicas", publisher="RTVA"))
    assert d.ownership == "publica" and d.public_source == "https://x.test/src"
    assert derive(_media(mtype="Televisión y radio públicas")).ownership == "sin-grupo"  # no cited publisher


def test_rule_religious_only_from_cee_owner_or_religious_group():
    chain = {"owners": ["Ábside"], "owners_qids": ["Q1"], "source": "u",
             "owner_parents": [{"qid": "Q1", "owned_by_qids": ["Q1518168"], "owned_by": ["CEE"]}]}
    d = derive(_media(facts=chain))
    assert d.religious_text == "Conferencia Episcopal Española"
    assert derive(_media(group=_group("Institución religiosa", "CEE"))).religious_text == "CEE"
    other = {"owners": ["ACdP"], "owners_qids": ["Q2"], "source": "u"}
    assert derive(_media(facts=other)).religious_text is None


def test_rule_no_group():
    assert derive(_media()).no_group
    assert not derive(_media(group=_group("Grupo editorial"))).no_group
    owned = {"owners": ["Co"], "owners_qids": ["Q1"], "owner_parents": [{"qid": "Q1", "owned_by": ["Holding"]}]}
    assert not derive(_media(facts=owned)).no_group
    assert NO_GROUP_TEXT == "No pertenece a ningún grupo de comunicación registrado en nuestro catálogo"


def test_real_catalog_derivations():
    cat = get_catalog()
    assert cat.media["rtve"].derived.ownership == "publica"
    assert cat.media["canal-sur-television"].derived.ownership == "publica"
    assert cat.media["cope"].derived.religious_text
    assert cat.media["el-pais"].derived.ownership == "grupo"
    assert "catalán" in cat.media["la-vanguardia"].derived.languages
    for m in cat.media.values():
        for row in m.ficha:
            assert row.value
