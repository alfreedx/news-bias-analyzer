"""Media and group catalog loaded from the versioned JSON files in data/."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .facts import Derived, FichaRow, derive, ficha
from .owners import owner_view, owner_views_for_facts
from .perception import load_perception, position_of
from .scoring import POSITION_LABELS, Assessment, assess

NO_OWNERSHIP_DATA = "No consta en las fuentes públicas que hemos consultado."


@dataclass(frozen=True)
class OwnershipRow:
    """One cited ownership fact: text, the source it comes from and the date that source states."""

    kind: str  # accionista | propia | financiacion | publico
    text: str
    source_title: str
    url: str
    date: str  # ISO date
    date_kind: str  # "datos" (date stated by the source) | "consulta" (source states none: date we consulted it)
    qid: str | None = None  # reviewed Wikidata item of the named shareholder, only when the match is certain

    @property
    def date_label(self) -> str:
        y, m, d = self.date.split("-")
        shown = f"{d}/{m}/{y}"
        return f"datos a {shown}" if self.date_kind == "datos" else f"consultado el {shown}"


@dataclass(frozen=True)
class Ownership:
    context: str | None = None
    rows: tuple[OwnershipRow, ...] = ()


def _load_ownership(directory: Path) -> tuple[dict[str, Ownership], dict[str, Ownership]]:
    """Cited ownership facts keyed by group id and by outlet id (data/ownership.json); empty if the file is absent."""
    path = directory / "ownership.json"
    if not path.exists():
        return {}, {}
    raw = json.loads(path.read_text(encoding="utf-8"))

    def build(section: dict) -> dict[str, Ownership]:
        return {
            key: Ownership(
                entry.get("context"),
                tuple(OwnershipRow(**row) for row in entry.get("rows", [])),
            )
            for key, entry in section.items()
        }

    return build(raw.get("groups", {})), build(raw.get("outlets", {}))


@dataclass
class Group:
    id: str
    name: str
    kind: str
    owners: str
    sources: list[dict]
    media: list[Media] = field(default_factory=list)
    ownership: Ownership = field(default_factory=Ownership)


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
    speciality: str | None = None  # factual: the subject the outlet itself is about (e.g. «economía»); never a stance
    facts: dict = field(default_factory=dict)  # Wikidata facts (scripts/enrich_media.py); empty when unknown
    group: Group | None = None
    ownership: Ownership = field(default_factory=Ownership)
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
            "speciality": self.speciality,
            "tags": [{"label": t.label, "source": t.source, "url": t.permalink, "axis": t.axis,
                      "retrieved": t.retrieved} for t in a.tags],
            "ideology": {"score": None if a.by_perception else a.score, "position": a.position, "label": a.label,
                         "agreement": a.agreement,
                         "basis": "perception" if a.by_perception else "labels" if a.rated else None},
            "perception": [{"medium": e.cis_name, "kind": e.kind, "question": e.question, "mean": e.mean,
                            "sd": e.sd, "n": e.n} for e in a.perception],
            "territorial": a.territorial,
            "factual": {"level": a.factual_level, "label": a.factual_label},
            "ifcn": a.ifcn,
            "founded": self.founded,
            "languages": self.derived.languages,
            "ownership": self.derived.ownership,
            "wikidata": self.facts.get("source"),
            "ownership_facts": ownership_public(self.ownership),
            "owners": owner_views_for_facts(self.facts),
        }


def ownership_public(own: Ownership) -> dict:
    return {
        "context": own.context,
        "rows": [
            {"text": r.text, "source": r.source_title, "url": r.url, "date": r.date, "date_kind": r.date_kind,
             "owner": owner_view(r.qid)}
            for r in own.rows
        ],
        "if_empty": None if own.rows else NO_OWNERSHIP_DATA,
    }


class Catalog:
    def __init__(self, directory: Path):
        groups_raw = json.loads((directory / "groups.json").read_text(encoding="utf-8"))["groups"]
        media_raw = json.loads((directory / "media.json").read_text(encoding="utf-8"))["media"]

        self.groups: dict[str, Group] = {
            g["id"]: Group(g["id"], g["name"], g["kind"], g["owners"], g.get("sources", [])) for g in groups_raw
        }
        group_own, media_own = _load_ownership(directory)
        for gid, own in group_own.items():
            self.groups[gid].ownership = own  # KeyError = ownership.json names an unknown group
        self.media: dict[str, Media] = {}
        self.perception_meta: dict = {}
        self.perception_unmapped: tuple = ()
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
                speciality=raw.get("speciality"),
                facts=raw.get("facts", {}),
            )
            if item.id in self.media:
                raise ValueError(f"Duplicate media id: {item.id}")
            if item.group_id:
                item.group = self.groups[item.group_id]  # KeyError = broken catalog, fail fast
                item.group.media.append(item)
            item.ownership = media_own.pop(item.id, Ownership())
            item.assessment = assess(item.evidence)
            item.derived = derive(item)
            self.media[item.id] = item
            for domain in item.domains:
                self._domain_index[domain] = item
        if media_own:
            raise ValueError(f"ownership.json names unknown outlets: {sorted(media_own)}")
        perception = load_perception(directory, set(self.media))  # unknown ids raise
        self.perception_meta = perception.meta
        self.perception_unmapped = perception.unmapped
        for oid, entries in perception.media.items():
            a = self.media[oid].assessment
            a.perception = entries
            if a.position is None and (pos := position_of(entries)) is not None:
                # No published label: fall back to the CIS band (never mixed with labels).
                a.position, a.score, a.label, a.by_perception = pos, float(pos), POSITION_LABELS[pos], True

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
