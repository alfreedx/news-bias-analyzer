"""Sourced facts about an outlet and the classifications derived from them.

Everything here is computed in code from two kinds of cited data and shown, with its rule, on
/metodologia. Nothing is an editorial judgement and nothing is turned into an ideological adjective:

* ``facts`` stored in data/media.json (Wikidata, CC0), written by scripts/enrich_media.py;
* the catalog itself: the registered group (groups.json, each with its own sources) and the cited publisher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .catalog import Media

# Wikidata items used by the rules. Listed here (and on /metodologia) so anyone can audit them.
PUBLIC_KIND_QIDS: dict[str, str] = {
    "Q26398": "radiodifusión pública",
    "Q1126006": "emisora pública",
    "Q270791": "empresa estatal",
    "Q133699310": "sociedad mercantil estatal",
}
PUBLIC_OWNER_QIDS: dict[str, str] = {"Q1194749": "Gobierno de España"}
RELIGIOUS_OWNER_QIDS: dict[str, str] = {"Q1518168": "Conferencia Episcopal Española"}
# Catalog values (each is cited in groups.json / owner_source) that also mark public ownership or a religious owner.
PUBLIC_GROUP_KIND_PREFIXES = ("Medio público", "Agencia pública")
PUBLIC_TYPE_PREFIXES = ("Televisión y radio públicas", "Agencia pública")
RELIGIOUS_GROUP_KINDS = ("Institución religiosa",)

OWNERSHIP_LABELS = {
    "publica": "Titularidad pública",
    "grupo": "Grupo de comunicación",
    "sin-grupo": "Sin grupo registrado",
}

NO_GROUP_TEXT = "No pertenece a ningún grupo de comunicación registrado en nuestro catálogo"


# Wikidata "instance of" values that describe a TV show, a company form or a generic entity rather
# than the kind of outlet; they are kept in the data but not shown as "Tipo".
NON_MEDIA_KINDS = {"programa de televisión", "serie de televisión", "editorial", "organización",
                   "medio de comunicación de masas", "business newspaper", "empresa estatal",
                   "sociedad mercantil estatal", "organización sin fines de lucro"}
# Not a language: Wikidata uses it for outlets that publish in several.
NON_LANGUAGES = {"plurilingüe", "multilingüe"}

@dataclass
class Derived:
    ownership: str = "sin-grupo"  # publica | grupo | sin-grupo
    public_text: str | None = None
    public_source: str | None = None
    public_via: str | None = None  # "wikidata" | "grupo" | "editor"
    religious_text: str | None = None
    religious_source: str | None = None
    no_group: bool = False
    languages: list[str] = field(default_factory=list)

    @property
    def ownership_label(self) -> str:
        return OWNERSHIP_LABELS[self.ownership]


def _wikidata_public_owner(facts: dict) -> str | None:
    """Name of the public body behind the outlet according to Wikidata, if any (owner or the owner's owner)."""
    kinds = set(facts.get("kinds_qids", []))
    owners = dict(zip(facts.get("owners_qids", []), facts.get("owners", []), strict=False))
    for qid, label in owners.items():
        if qid in PUBLIC_OWNER_QIDS:
            return label
    for parent in facts.get("owner_parents", []):
        if parent["qid"] in PUBLIC_OWNER_QIDS or set(parent.get("owned_by_qids", [])) & PUBLIC_OWNER_QIDS.keys():
            return parent.get("label") or owners.get(parent["qid"])
        if set(parent.get("kinds_qids", [])) & PUBLIC_KIND_QIDS.keys():
            return parent.get("label") or owners.get(parent["qid"])
    if kinds & PUBLIC_KIND_QIDS.keys():
        return ", ".join(facts.get("owners", [])[:1]) or "entidad pública"
    return None


def _wikidata_religious_owner(facts: dict) -> str | None:
    owners = dict(zip(facts.get("owners_qids", []), facts.get("owners", []), strict=False))
    for qid in owners:
        if qid in RELIGIOUS_OWNER_QIDS:
            return RELIGIOUS_OWNER_QIDS[qid]
    for parent in facts.get("owner_parents", []):
        for qid in parent.get("owned_by_qids", []):
            if qid in RELIGIOUS_OWNER_QIDS:
                return RELIGIOUS_OWNER_QIDS[qid]
    return None


def derive(media: Media) -> Derived:
    """Apply the documented rules to one outlet."""
    facts = media.facts or {}
    d = Derived(languages=[x for x in facts.get("languages", []) if x.lower() not in NON_LANGUAGES])
    group = media.group

    # Public ownership: Wikidata first, then the cited catalog data.
    owner = _wikidata_public_owner(facts) if facts else None
    if owner:
        d.public_text, d.public_source, d.public_via = owner, facts.get("source"), "wikidata"
    elif group and group.kind.startswith(PUBLIC_GROUP_KIND_PREFIXES):
        d.public_text, d.public_source, d.public_via = group.name, f"/grupos/{group.id}", "grupo"
    elif media.type.startswith(PUBLIC_TYPE_PREFIXES) and media.publisher:
        d.public_text, d.public_source, d.public_via = media.publisher, media.owner_source, "editor"

    # Religious owner (stated as a fact about the owner, nothing more).
    religious = _wikidata_religious_owner(facts) if facts else None
    if religious:
        d.religious_text, d.religious_source = religious, facts.get("source")
    elif group and group.kind in RELIGIOUS_GROUP_KINDS:
        d.religious_text, d.religious_source = group.name, f"/grupos/{group.id}"

    if d.public_text:
        d.ownership = "publica"
    elif group:
        d.ownership = "grupo"
    else:
        # no registered group and Wikidata shows no owner that is itself owned by someone else
        parents_owned = any(p.get("owned_by") for p in facts.get("owner_parents", []))
        d.ownership = "grupo" if parents_owned else "sin-grupo"
    d.no_group = d.ownership == "sin-grupo"
    return d


@dataclass
class FichaRow:
    label: str
    value: str
    source: str | None = None  # permalink; the template adds «Wikidata · fecha» for Wikidata links
    source_name: str | None = None
    retrieved: str | None = None
    href: str | None = None  # the value itself links here (e.g. the group page)


def ficha(media: Media) -> list[FichaRow]:
    """Compact definition list shown on the outlet page. Each row carries its source when it has one."""
    facts = media.facts or {}
    wd = facts.get("source")
    when = facts.get("retrieved")
    d = media.derived
    rows: list[FichaRow] = []

    def wikidata(label: str, value: str) -> None:
        rows.append(FichaRow(label, value, wd, "Wikidata", when))

    if media.founded:
        if facts.get("founded") == media.founded:
            wikidata("Fundado", str(media.founded))
        else:
            rows.append(FichaRow("Fundado", str(media.founded)))
    kinds = [k for k in facts.get("kinds", []) if k.lower() not in NON_MEDIA_KINDS]
    if kinds:
        wikidata("Tipo", ", ".join(kinds[:3]))
    else:
        rows.append(FichaRow("Tipo", media.type))
    langs = [x for x in facts.get("languages", []) if x.lower() not in NON_LANGUAGES]
    if langs:
        wikidata("Lengua", ", ".join(langs))
    if facts.get("hq"):
        wikidata("Sede", facts["hq"])
    if facts.get("owners"):
        wikidata("Propiedad / editor", ", ".join(facts["owners"]))
    rows.append(FichaRow("Ámbito", media.scope + (f" ({media.region})" if media.region else "")))

    if d.public_text:
        src = d.public_source
        is_wd = bool(src) and src.startswith("https://www.wikidata.org/")
        text = (f"Medio de titularidad pública: {d.public_text}" if "(" in d.public_text
                else f"Medio de titularidad pública ({d.public_text})")
        rows.append(FichaRow("Titularidad", text, src, "Wikidata" if is_wd else "Fuente",
                             when if d.public_via == "wikidata" else None))
    elif d.no_group:
        rows.append(FichaRow("Grupo", NO_GROUP_TEXT))
    if d.religious_text:
        rows.append(FichaRow("Propietario religioso",
                             f"Propiedad de una institución religiosa: {d.religious_text}", d.religious_source,
                             "Wikidata" if d.religious_source == wd else "Fuente",
                             when if d.religious_source == wd else None))
    return rows
