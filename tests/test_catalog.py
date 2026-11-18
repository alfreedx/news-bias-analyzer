"""Integrity checks for the versioned data files."""

from urllib.parse import urlsplit

import pytest

from app.catalog import get_catalog
from app.scoring import FACTUAL_MAP, IDEOLOGY_MAP, TERRITORIAL_LABELS, _norm

catalog = get_catalog()
ALLOWED_SOURCES = {"mbfc", "wikipedia", "wikidata"}


def test_catalog_loads():
    assert len(catalog.media) >= 50
    assert catalog.factcheckers


def test_domains_are_unique():
    seen = {}
    for m in catalog.media.values():
        for d in m.domains:
            assert d not in seen, f"{d} used by {seen.get(d)} and {m.id}"
            seen[d] = m.id


@pytest.mark.parametrize("media", list(catalog.media.values()), ids=lambda m: m.id)
def test_evidence_is_well_formed(media):
    for item in media.evidence:
        assert item["source"] in ALLOWED_SOURCES
        assert item["kind"] in {"ideology", "factual", "ifcn"}
        assert urlsplit(item["url"]).scheme == "https"
        for label in item["labels"]:
            key = _norm(label)
            if item["kind"] == "ideology":
                assert key in IDEOLOGY_MAP or key in TERRITORIAL_LABELS, f"unmapped label {label!r}"
            elif item["kind"] == "factual":
                assert key in FACTUAL_MAP, f"unmapped factual label {label!r}"
    for feed in media.feeds:
        assert urlsplit(feed).scheme == "https"


def test_group_sources_are_https():
    for g in catalog.groups.values():
        assert g.sources, f"group {g.id} has no sources"
        for s in g.sources:
            assert urlsplit(s["url"]).scheme == "https"


def test_url_matching_uses_longest_suffix():
    assert catalog.by_url("https://cincodias.elpais.com/x").id == "cinco-dias"
    assert catalog.by_url("https://elpais.com/espana/x.html").id == "el-pais"
    assert catalog.by_url("https://www.elmundo.es/a").id == "el-mundo"
    assert catalog.by_url("https://elpais.com.evil.example/a") is None
    assert catalog.by_url("not a url") is None
