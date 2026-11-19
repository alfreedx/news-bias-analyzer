"""Media and group catalog loaded from the versioned JSON files in data/."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .facts import Derived, FichaRow, derive, ficha
from .scoring import Assessment, assess


@dataclass
class Group:
    id: str
    name: str
    kind: str
    owners: str
    sources: list[dict]
    media: list[Media] = field(default_factory=list)


@dataclass
class Media:
    id: str
    name: str
    domains: list[str]
    type: str
    scope: str
    founded: int | None
    group_id: str | None
    feeds: list[str]
    evidence: list[dict]
    region: str | None = None
    publisher: str | None = None
    factchecker: bool = False
    owner_source: str | None = None
    facts: dict = field(default_factory=dict)  # Wikidata facts (scripts/enrich_media.py); empty when unknown
    group: Group | None = None
    assessment: Assessment = field(default_factory=Assessment)
    derived: Derived = field(default_factory=Derived)

    @property
    def ficha(self) -> list[FichaRow]:
        return ficha(self)

    def to_public(self) -> dict:
        a = self.assessment
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "scope": self.scope,
            "region": self.region,
            "group": self.group.name if self.group else None,
            "group_id": self.group.id if self.group else None,
            "publisher": self.publisher,
            "factchecker": self.factchecker,
            "tags": [{"label": t.label, "source": t.source, "url": t.permalink, "axis": t.axis,
                      "retrieved": t.retrieved} for t in a.tags],
            "ideology": {"score": a.score, "position": a.position, "label": a.label, "agreement": a.agreement},
            "territorial": a.territorial,
            "factual": {"level": a.factual_level, "label": a.factual_label},
            "ifcn": a.ifcn,
            "founded": self.founded,
            "languages": self.derived.languages,
            "ownership": self.derived.ownership,
            "wikidata": self.facts.get("source"),
        }


class Catalog:
    def __init__(self, directory: Path):
        groups_raw = json.loads((directory / "groups.json").read_text(encoding="utf-8"))["groups"]
        media_raw = json.loads((directory / "media.json").read_text(encoding="utf-8"))["media"]

        self.groups: dict[str, Group] = {
            g["id"]: Group(g["id"], g["name"], g["kind"], g["owners"], g.get("sources", [])) for g in groups_raw
        }
        self.media: dict[str, Media] = {}
        self._domain_index: dict[str, Media] = {}

        for raw in media_raw:
            item = Media(
                id=raw["id"],
                name=raw["name"],
                domains=[d.lower() for d in raw["domains"]],
                type=raw["type"],
                scope=raw["scope"],
                founded=raw.get("founded"),
                group_id=raw.get("group"),
                feeds=raw.get("feeds", []),
                evidence=raw.get("evidence", []),
                region=raw.get("region"),
                publisher=raw.get("publisher"),
                factchecker=raw.get("factchecker", False),
                owner_source=raw.get("owner_source"),
                facts=raw.get("facts", {}),
            )
            if item.id in self.media:
                raise ValueError(f"Duplicate media id: {item.id}")
            if item.group_id:
                item.group = self.groups[item.group_id]  # KeyError = broken catalog, fail fast
                item.group.media.append(item)
            item.assessment = assess(item.evidence)
            item.derived = derive(item)
            self.media[item.id] = item
            for domain in item.domains:
                self._domain_index[domain] = item

    def by_url(self, url: str) -> Media | None:
        """Find the outlet that owns a URL, matching the longest known domain suffix."""
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return None
        parts = host.split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in self._domain_index:
                return self._domain_index[candidate]
        return None

    @property
    def news_media(self) -> list[Media]:
        return [m for m in self.media.values() if not m.factchecker]

    @property
    def factcheckers(self) -> list[Media]:
        return [m for m in self.media.values() if m.factchecker]


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    return Catalog(settings.catalog_dir)
