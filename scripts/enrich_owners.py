"""Write data/owners.json: a short, sourced description of every owner or shareholder, read from Wikidata (CC0).

Which items are described:
  * the owners and owners-of-owners already stored in data/media.json (``facts.owners_qids``,
    ``facts.owner_parents[].qid`` and ``owned_by_qids``);
  * the shareholders of data/ownership.json that carry an explicit, reviewed ``qid`` field.

Only professional / institutional facts are stored (data minimisation). NEVER stored: date of birth,
family, addresses, wealth, or political-party membership (P102, and positions that are party offices).

    person        P106 occupation, P39 public positions with start/end years, P108 employers, P27 country
    organisation  P31 kind, P452 sector, P17 country, P159 headquarters, P749/P127 parent, P571 inception year

Polite by design: batches of <= 50 ids, 1 s pause between batches, back-off on 403/429/5xx, descriptive
User-Agent without personal data. If a batch cannot be fetched the previous entries are kept.

Usage:  python scripts/enrich_owners.py [--dry-run]
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
MEDIA_FILE = ROOT / "data" / "media.json"
OWNERSHIP_FILE = ROOT / "data" / "ownership.json"
OUT_FILE = ROOT / "data" / "owners.json"
UA = "NewsBiasAnalyzer-enrich/1.0 (open-source data script)"
API = "https://www.wikidata.org/w/api.php"
BATCH = 50
QID_RE = re.compile(r"^Q\d+$")
LANGS = ("es", "en")
MAX_POSITIONS = 5
MAX_EMPLOYERS = 3
# Position labels that are party offices: never shown (party membership would be political profiling).
PARTY_WORDS = re.compile(r"\b(partido|party|parti|partit|militante|afiliad[oa])\b", re.IGNORECASE)
GENERIC_KINDS = {"organización", "organizacion", "negocio", "empresa", "organization", "business"}
META = {
    "description": "Descripción breve de propietarios y accionistas de los medios, leída de Wikidata. Solo datos "
                   "profesionales o institucionales; no se guardan fecha de nacimiento, familia, domicilio, "
                   "patrimonio ni militancia en partidos.",
    "source": "Wikidata",
    "license": "CC0",
}


def request(params: dict) -> dict | None:
    """GET JSON from the Wikidata API with back-off on throttling; None when it keeps failing."""
    full = f"{API}?{urlencode(params)}"
    for attempt in range(5):
        status = None
        try:
            with urlopen(Request(full, headers={"User-Agent": UA}), timeout=30) as r:  # noqa: S310 (fixed https host)
                return json.load(r)
        except HTTPError as exc:
            status = exc.code
            if status not in (403, 429, 500, 502, 503):
                return None
        except (URLError, TimeoutError, ValueError):
            status = "network error"
        print(f"  retry {attempt + 1} ({status})", flush=True)
        time.sleep(5 * 2**attempt)
    return None


def chunks(items: list, n: int = BATCH):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# --- pure parsing (unit-tested) ---------------------------------------------------
def pick(texts: dict) -> tuple[str | None, str | None]:
    """(text, language) in the preferred language order es, en."""
    for lang in LANGS:
        value = (texts.get(lang) or {}).get("value")
        if value:
            return value, lang
    return None, None


def claims_of(entity: dict, prop: str) -> list[dict]:
    """Non-deprecated claims with a real value."""
    return [c for c in entity.get("claims", {}).get(prop, [])
            if c.get("rank") != "deprecated" and c.get("mainsnak", {}).get("snaktype") == "value"]


def item_id(claim: dict) -> str | None:
    value = claim["mainsnak"]["datavalue"]["value"]
    if isinstance(value, dict) and QID_RE.match(value.get("id", "")):
        return value["id"]
    return None


def year_of(claim: dict, prop: str) -> int | None:
    """Year of a time qualifier (P580 start, P582 end) when Wikidata gives at least year precision."""
    for q in claim.get("qualifiers", {}).get(prop, []):
        if q.get("snaktype") != "value":
            continue
        value = q["datavalue"]["value"]
        m = re.match(r"^\+?(\d{4})", value.get("time", ""))
        if m and value.get("precision", 0) >= 9:
            return int(m.group(1))
    return None


def current_ids(entity: dict, prop: str) -> list[str]:
    """Item ids of claims with no end time: what Wikidata says holds today."""
    out: list[str] = []
    for c in claims_of(entity, prop):
        if "P582" in c.get("qualifiers", {}):
            continue
        qid = item_id(c)
        if qid and qid not in out:
            out.append(qid)
    return out


def position_body_ids(claim: dict) -> list[str]:
    """Items naming the body of a position: qualifiers P2389 (organization or body), P108 (employer), P642 (of)."""
    out: list[str] = []
    for prop in ("P2389", "P108", "P642"):
        for q in claim.get("qualifiers", {}).get(prop, []):
            value = q["datavalue"]["value"] if q.get("snaktype") == "value" else None
            if isinstance(value, dict) and QID_RE.match(value.get("id", "")):
                out.append(value["id"])
    return out


def referenced_ids(entity: dict) -> set[str]:
    """Every item whose label is needed to render this entity."""
    ids: set[str] = set()
    for prop in ("P31", "P106", "P39", "P108", "P27", "P452", "P17", "P159", "P749", "P127"):
        for c in claims_of(entity, prop):
            qid = item_id(c)
            if qid:
                ids.add(qid)
            ids.update(position_body_ids(c))
    return ids


def dated(entity: dict, prop: str, labels: dict[str, str], cap: int, skip_party: bool = False) -> list[dict]:
    """[{label, start, end}] newest first (undated last); items without a label are skipped."""
    out: list[dict] = []
    for c in claims_of(entity, prop):
        qid = item_id(c)
        label = labels.get(qid or "")
        if not label or (skip_party and PARTY_WORDS.search(label)):
            continue
        if prop == "P39":
            body = next((labels[b] for b in position_body_ids(c) if labels.get(b)), None)
            if body:
                label = f"{label} de {body}"
        row = {"label": label, "start": year_of(c, "P580"), "end": year_of(c, "P582")}
        if row not in out:
            out.append(row)
    out.sort(key=lambda r: (r["start"] is None, -(r["start"] or 0)))
    return out[:cap]


def labelled(entity: dict, prop: str, labels: dict[str, str], cap: int = 3, current: bool = False) -> list[str]:
    ids = current_ids(entity, prop) if current else [i for c in claims_of(entity, prop) if (i := item_id(c))]
    out: list[str] = []
    for qid in ids:
        text = labels.get(qid)
        if text and "wikidata" not in text.lower() and text not in out:  # skip Wikidata maintenance labels
            out.append(text)
    specific = [t for t in out if t.lower() not in GENERIC_KINDS]
    return (specific or out)[:cap]


def inception_year(entity: dict) -> int | None:
    for c in claims_of(entity, "P571"):
        value = c["mainsnak"]["datavalue"]["value"]
        m = re.match(r"^\+?(\d{4})", value.get("time", ""))
        if m and value.get("precision", 0) >= 9:
            return int(m.group(1))
    return None


def build_owner(qid: str, entity: dict, labels: dict[str, str], retrieved: str) -> dict | None:
    """One owners.json entry, or None when the item has no label at all."""
    label, _ = pick(entity.get("labels", {}))
    if not label:
        return None
    description, desc_lang = pick(entity.get("descriptions", {}))
    is_person = "Q5" in [item_id(c) for c in claims_of(entity, "P31")]
    entry: dict = {"label": label, "description": description, "description_lang": desc_lang,
                   "kind": "person" if is_person else "organisation",
                   "url": f"https://www.wikidata.org/wiki/{qid}", "retrieved": retrieved}
    if is_person:
        entry["occupations"] = labelled(entity, "P106", labels, cap=4)
        entry["positions"] = dated(entity, "P39", labels, MAX_POSITIONS, skip_party=True)
        entry["employers"] = dated(entity, "P108", labels, MAX_EMPLOYERS)
        countries = labelled(entity, "P27", labels, cap=1)
        entry["country"] = countries[0] if countries else None
    else:
        entry["instance_of"] = labelled(entity, "P31", labels, cap=3)
        entry["industry"] = labelled(entity, "P452", labels, cap=3)
        countries = labelled(entity, "P17", labels, cap=1)
        entry["country"] = countries[0] if countries else None
        hq = labelled(entity, "P159", labels, cap=1, current=True)
        entry["hq"] = hq[0] if hq else None
        parents = current_ids(entity, "P749") or current_ids(entity, "P127")
        entry["parent"] = [{"qid": p, "label": labels[p]} for p in parents if labels.get(p)][:3]
        entry["inception"] = inception_year(entity)
    return entry


# --- collecting the QIDs ------------------------------------------------------------
def owner_qids() -> list[str]:
    """QIDs from media.json (owners, owner parents, their owners) and the reviewed ownership.json rows."""
    found: list[str] = []

    def add(q: str) -> None:
        if QID_RE.match(q) and q not in found:
            found.append(q)

    for m in json.loads(MEDIA_FILE.read_text(encoding="utf-8"))["media"]:
        facts = m.get("facts") or {}
        for q in facts.get("owners_qids", []):
            add(q)
        for p in facts.get("owner_parents", []):
            add(p["qid"])
            for q in p.get("owned_by_qids", []):
                add(q)
    raw = json.loads(OWNERSHIP_FILE.read_text(encoding="utf-8"))
    for section in ("groups", "outlets"):
        for entry in raw.get(section, {}).values():
            for row in entry.get("rows", []):
                if row.get("qid"):
                    add(row["qid"])
    return found


def fetch(ids: list[str], props: str, languages: str = "es|en") -> dict[str, dict] | None:
    """wbgetentities for up to 50 ids; None when the API fails."""
    data = request({"action": "wbgetentities", "ids": "|".join(ids), "props": props,
                    "languages": languages, "format": "json"})
    if not data or "entities" not in data:
        return None
    return {k: v for k, v in data["entities"].items() if "missing" not in v}


def main() -> int:
    dry = "--dry-run" in sys.argv
    retrieved = date.today().isoformat()
    previous = json.loads(OUT_FILE.read_text(encoding="utf-8"))["owners"] if OUT_FILE.exists() else {}
    qids = owner_qids()
    print(f"{len(qids)} owner items to describe")

    entities: dict[str, dict] = {}
    failed: list[str] = []
    for batch in chunks(qids):
        got = fetch(batch, "labels|descriptions|claims")
        if got is None:
            failed += batch
        else:
            entities.update(got)
        time.sleep(1)

    needed = sorted(set().union(*(referenced_ids(e) for e in entities.values())) if entities else set())
    labels: dict[str, str] = {}
    for batch in chunks(needed):
        got = fetch(batch, "labels")
        if got is None:
            print("  labels could not be fetched for a batch; keeping previous data for everything", flush=True)
            failed += qids
            break
        for q, e in got.items():
            text, _ = pick(e.get("labels", {}))
            if text:
                labels[q] = text
        time.sleep(1)

    owners: dict[str, dict] = {}
    for qid in qids:
        entry = None
        if qid in entities and qid not in failed:
            entry = build_owner(qid, entities[qid], labels, retrieved)
        if entry is None and qid in previous:
            entry = previous[qid]  # keep what we had rather than lose it on a failed run
        if entry is not None:
            owners[qid] = entry
    persons = sum(o["kind"] == "person" for o in owners.values())
    print(f"{len(owners)} described: {persons} persons, {len(owners) - persons} organisations; "
          f"{len(failed)} not refreshed; {len(qids) - len(owners)} without data")
    if dry:
        return 0
    if failed and not owners:
        print("nothing written", file=sys.stderr)
        return 1
    out = {"_meta": {**META, "retrieved": retrieved}, "owners": owners}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
