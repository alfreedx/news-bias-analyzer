"""Short, sourced descriptions of owners and shareholders (data/owners.json, Wikidata, CC0).

The file is written by scripts/enrich_owners.py and holds only professional or institutional facts.
Nothing is inferred here: this module only formats what Wikidata says into plain Spanish, and returns
nothing when the file or the entry is missing (the page then shows the name alone).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .config import settings

OWNERS_FILE = "owners.json"


@lru_cache(maxsize=4)
def _load(directory: str) -> dict:
    path = Path(directory) / OWNERS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    owners = raw.get("owners") if isinstance(raw, dict) else None
    return owners if isinstance(owners, dict) else {}


def load_owners(directory: Path | None = None) -> dict:
    """Owners keyed by QID; empty when data/owners.json is missing or unreadable."""
    return _load(str(directory or settings.catalog_dir))


def years(start: int | None, end: int | None) -> str:
    """«desde 2014», «2010–2016», «hasta 2016», «2014» or an empty string."""
    if start and end:
        return str(start) if start == end else f"{start}–{end}"
    if start:
        return f"desde {start}"
    if end:
        return f"hasta {end}"
    return ""


def _with_years(row: dict) -> str:
    span = years(row.get("start"), row.get("end"))
    return f"{row['label']} ({span})" if span else row["label"]


def _date_label(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def owner_view(qid: str | None) -> dict | None:
    """Everything a page needs to describe one owner, already in Spanish; None when we have nothing."""
    entry = load_owners().get(qid or "")
    if not entry or not entry.get("label"):
        return None
    facts: list[dict[str, str]] = []

    def add(label: str, values: list[str]) -> None:
        values = [v for v in values if v]
        if values:
            facts.append({"label": label, "value": ", ".join(values)})

    if entry.get("kind") == "person":
        add("Ocupación", entry.get("occupations", []))
        add("Cargos", [_with_years(p) for p in entry.get("positions", [])])
        add("Trabajo", [_with_years(p) for p in entry.get("employers", [])])
        add("Ciudadanía", [entry.get("country")])
    else:
        add("Tipo", entry.get("instance_of", []))
        add("Sector", entry.get("industry", []))
        add("País", [entry.get("country")])
        add("Sede", [entry.get("hq")])
        add("Matriz o propietario", [p["label"] for p in entry.get("parent", [])])
        add("Fundación", [str(entry["inception"])] if entry.get("inception") else [])
    description = entry.get("description")
    retrieved = entry.get("retrieved") or ""
    return {
        "qid": qid,
        "name": entry["label"],
        "kind": entry.get("kind"),
        "description": description,
        "description_lang": entry.get("description_lang"),
        "facts": facts,
        "source": entry.get("url") or f"https://www.wikidata.org/wiki/{qid}",
        "retrieved": retrieved,
        "date_label": _date_label(retrieved) if retrieved else "",
        "parents": [p for p in entry.get("parent", []) if p.get("qid")],
    }


def owner_views_for_facts(facts: dict) -> list[dict]:
    """Descriptions of an outlet's Wikidata owners, then of the owners of those owners (no repeats)."""
    qids: list[str] = list(facts.get("owners_qids", []))
    for parent in facts.get("owner_parents", []):
        qids.append(parent.get("qid", ""))
        qids.extend(parent.get("owned_by_qids", []))
    seen: set[str] = set()
    out = []
    for qid in qids:
        if qid in seen:
            continue
        seen.add(qid)
        view = owner_view(qid)
        if view:
            out.append(view)
    return out
