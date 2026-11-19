"""Enrich data/media.json with sourced facts read from Wikidata (CC0).

For every outlet this script resolves a Wikidata item and stores, under the key
``facts``, only values that Wikidata actually publishes (nothing is guessed):

    qid, languages (P407), kinds (P31), owners (P127 + P123), hq (P159),
    source (permalink of the item), retrieved (date)

``founded`` (P571) is filled only when the catalog does not have it yet.
The item is found, in this order, from (1) a Wikidata URL already cited as
evidence / owner_source, (2) the outlet's es.wikipedia article (pageprops).
Items whose official website (P856) points to a different domain are NOT
written: they are printed for manual review instead.

Polite by design: batched API calls, 1 s pause between batches, back-off on
403/429/5xx, descriptive User-Agent without personal data.

Usage:  python scripts/enrich_media.py [--dry-run] [--search-names]

--search-names also tries outlets with no Wikidata/Wikipedia citation: they are searched by name and
accepted only when exactly one candidate lists the outlet's own domain as official website (P856).
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
MEDIA_FILE = ROOT / "data" / "media.json"
UA = "NewsBiasAnalyzer-enrich/1.0 (open-source data script)"
API = "https://www.wikidata.org/w/api.php"
BATCH = 50
QID_RE = re.compile(r"^Q\d+$")
# (property, key in facts, does an end-time qualifier (P582) mean "no longer true"?)
LIST_PROPS = {"P407": "languages", "P31": "kinds", "P127": "owners", "P123": "owners", "P159": "hq"}


def request(url: str, params: dict) -> dict | None:
    """GET JSON (stdlib urllib) with polite back-off on throttling (403/429/5xx)."""
    full = f"{url}?{urlencode(params)}"
    for attempt in range(5):
        status = None
        try:
            with urlopen(Request(full, headers={"User-Agent": UA}), timeout=30) as r:  # noqa: S310 (fixed https hosts)
                return json.load(r)
        except HTTPError as exc:
            status = exc.code
            if status not in (403, 429, 500, 502, 503):
                return None
        except (URLError, TimeoutError, ValueError):
            status = "network error"
        print(f"  retry {attempt + 1} ({status}) {url.split('/')[2]}", flush=True)
        time.sleep(5 * 2**attempt)
    return None


def chunks(items: list, n: int = BATCH):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# --- pure parsing (unit-tested) ---------------------------------------------------
def current_claims(entity: dict, prop: str) -> list[dict]:
    """Claims that are not deprecated and have no end time (P582): what Wikidata says is true today."""
    out = []
    for c in entity.get("claims", {}).get(prop, []):
        if c.get("rank") == "deprecated":
            continue
        if "P582" in c.get("qualifiers", {}):
            continue
        if c.get("mainsnak", {}).get("snaktype") != "value":
            continue
        out.append(c)
    preferred = [c for c in out if c.get("rank") == "preferred"]
    return preferred or out


def item_ids(entity: dict, prop: str) -> list[str]:
    ids = []
    for c in current_claims(entity, prop):
        value = c["mainsnak"]["datavalue"]["value"]
        if isinstance(value, dict) and QID_RE.match(value.get("id", "")) and value["id"] not in ids:
            ids.append(value["id"])
    return ids


def inception_year(entity: dict) -> int | None:
    for c in current_claims(entity, "P571"):
        value = c["mainsnak"]["datavalue"]["value"]
        if value.get("precision", 0) >= 9:
            m = re.match(r"^\+?(\d{4})", value["time"])
            if m:
                return int(m.group(1))
    return None


def websites(entity: dict) -> list[str]:
    hosts = []
    for c in entity.get("claims", {}).get("P856", []):
        if c.get("mainsnak", {}).get("snaktype") == "value":
            host = (urlsplit(c["mainsnak"]["datavalue"]["value"]).hostname or "").lower()
            if host:
                hosts.append(host.removeprefix("www."))
    return hosts


def website_matches(entity: dict, domains: list[str]) -> bool | None:
    """True/False when Wikidata lists a website (exact host, ignoring "www."); None when it does not."""
    hosts = websites(entity)
    if not hosts:
        return None
    doms = {d.lower().removeprefix("www.") for d in domains}
    return any(h in doms for h in hosts)


def label_of(labels: dict, qid: str) -> str | None:
    return labels.get(qid)


def build_facts(qid: str, entity: dict, labels: dict[str, str], retrieved: str) -> dict:
    """Facts block for one outlet. Only values present in Wikidata, labels in Spanish."""
    facts: dict = {"qid": qid}
    for prop, key in LIST_PROPS.items():
        ids = item_ids(entity, prop)
        if not ids:
            continue
        if key == "hq":
            facts["hq"] = labels.get(ids[0], ids[0])
            facts["hq_qid"] = ids[0]
            continue
        facts.setdefault(key, [])
        facts.setdefault(key + "_qids", [])
        for i in ids:
            name = labels.get(i)
            if name and name not in facts[key]:
                facts[key].append(name)
                facts[key + "_qids"].append(i)
    year = inception_year(entity)
    if year:
        facts["founded"] = year
    facts["source"] = f"https://www.wikidata.org/wiki/{qid}"
    facts["retrieved"] = retrieved
    return facts


# --- Wikidata access --------------------------------------------------------------
def fetch_entities(ids: list[str], props: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for batch in chunks(sorted(set(ids))):
        data = request(API, {"action": "wbgetentities", "format": "json", "ids": "|".join(batch),
                                     "props": props, "languages": "es|en", "languagefallback": 1})
        if data:
            out.update(data.get("entities", {}))
        time.sleep(1)
    return out


def resolve_qids(media: list[dict]) -> dict[str, tuple[str, str]]:
    """media id -> (QID, how it was found)."""
    found: dict[str, tuple[str, str]] = {}
    titles: dict[str, str] = {}
    for m in media:
        urls = [ev["url"] for ev in m.get("evidence", [])] + ([m["owner_source"]] if m.get("owner_source") else [])
        for u in urls:
            p = urlsplit(u)
            q = p.path.rsplit("/", 1)[-1]
            if p.netloc == "www.wikidata.org" and QID_RE.match(q):
                found[m["id"]] = (q, "wikidata url")
                break
        if m["id"] in found:
            continue
        for u in urls:
            p = urlsplit(u)
            if p.netloc == "es.wikipedia.org" and p.path.startswith("/wiki/"):
                titles[unquote(p.path[len("/wiki/"):]).replace("_", " ")] = m["id"]
                break
    names = list(titles)
    for batch in chunks(names):
        data = request("https://es.wikipedia.org/w/api.php", {
            "action": "query", "format": "json", "redirects": 1, "prop": "pageprops",
            "ppprop": "wikibase_item", "titles": "|".join(batch)})
        time.sleep(1)
        if not data:
            continue
        q = data.get("query", {})
        alias = {n["from"]: n["to"] for n in q.get("normalized", []) + q.get("redirects", [])}
        items = {p["title"]: p.get("pageprops", {}).get("wikibase_item") for p in q.get("pages", {}).values()}
        for t in batch:
            name = t
            for _ in range(2):  # normalised, then redirected
                name = alias.get(name, name)
            qid = items.get(name)
            if qid and QID_RE.match(qid):
                found.setdefault(titles[t], (qid, "es.wikipedia pageprops"))
    return found


def search_by_website(media: list[dict]) -> dict[str, tuple[str, str]]:
    """Last resort for outlets with no Wikidata/Wikipedia citation.

    Searches Wikidata by the outlet's name and accepts a candidate ONLY when its official website (P856)
    is the outlet's own domain and exactly one candidate qualifies. A name alone is never enough.
    """
    found: dict[str, tuple[str, str]] = {}
    candidates: dict[str, list[str]] = {}
    for m in media:
        data = request(API, {"action": "wbsearchentities", "format": "json", "search": m["name"],
                                     "language": "es", "uselang": "es", "type": "item", "limit": 5})
        time.sleep(1)
        if data is None:  # still throttled after the back-off: stop instead of hammering the API
            print("  name search aborted (throttled)", flush=True)
            break
        ids = [h["id"] for h in (data or {}).get("search", []) if QID_RE.match(h.get("id", ""))]
        if ids:
            candidates[m["id"]] = ids
    entities = fetch_entities([i for ids in candidates.values() for i in ids], "claims")
    by_id = {m["id"]: m for m in media}
    for mid, ids in candidates.items():
        ok = [i for i in ids if i in entities and website_matches(entities[i], by_id[mid]["domains"]) is True]
        if len(ok) == 1:
            found[mid] = (ok[0], "name search + official website match")
    return found


def fetch_current(qids: list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (entities with claims, facts block rebuilt from today's Wikidata) keyed by QID.

    Each owner is also read one level up (its own P127 owners and P31 types), stored as ``owner_parents``;
    it lets the site tell, for example, that a company is itself owned by a public body.
    """
    entities = fetch_entities(qids, "claims")
    owner_ids = sorted({i for e in entities.values() for p in ("P127", "P123") for i in item_ids(e, p)})
    parents = fetch_entities(owner_ids, "claims")
    refs: set[str] = set()
    for ent in entities.values():
        for prop in LIST_PROPS:
            refs.update(item_ids(ent, prop))
    for ent in parents.values():
        for prop in ("P127", "P31"):
            refs.update(item_ids(ent, prop))
    labels_raw = fetch_entities(sorted(refs), "labels")
    labels = {q: e["labels"].get("es", e["labels"].get("en", {})).get("value")
              for q, e in labels_raw.items() if e.get("labels")}
    today = date.today().isoformat()
    facts = {}
    for q, e in entities.items():
        if "missing" in e:
            continue
        block = build_facts(q, e, labels, today)
        up = []
        for oid in block.get("owners_qids", []):
            pe = parents.get(oid)
            if not pe or "missing" in pe:
                continue
            owned_by = item_ids(pe, "P127")
            up.append({"qid": oid, "label": labels.get(oid), "kinds_qids": item_ids(pe, "P31"),
                       "owned_by": [labels[i] for i in owned_by if labels.get(i)], "owned_by_qids": owned_by})
        if up:
            block["owner_parents"] = up
        facts[q] = block
    return entities, facts


def main() -> int:
    dry = "--dry-run" in sys.argv
    doc = json.loads(MEDIA_FILE.read_text(encoding="utf-8"))
    media = doc["media"]
    qids = resolve_qids(media)
    print(f"QIDs from citations: {len(qids)}/{len(media)}", flush=True)
    if "--search-names" in sys.argv:
        qids.update(search_by_website([m for m in media if m["id"] not in qids]))
    print(f"QIDs resolved: {len(qids)}/{len(media)}", flush=True)
    entities, current = fetch_current([q for q, _ in qids.values()])

    written, review, used = 0, [], {}
    for m in media:
        if m["id"] not in qids:
            # Unresolved can just mean a throttled lookup: keep whatever was verified before.
            if m.get("facts"):
                review.append(f"{m['id']}: no QID resolved this run; previous facts kept")
            continue
        qid, how = qids[m["id"]]
        ent = entities.get(qid)
        if not ent:
            review.append(f"{m['id']}: {qid} could not be fetched ({how}); previous facts kept")
            continue
        if "missing" in ent:
            review.append(f"{m['id']}: {qid} not found in Wikidata ({how})")
            m.pop("facts", None)
            continue
        if website_matches(ent, m["domains"]) is False:
            review.append(f"{m['id']}: {qid} ({how}) official website {websites(ent)} != {m['domains']} -> skipped")
            m.pop("facts", None)
            continue
        year = inception_year(ent)
        if year and m.get("founded") and abs(year - m["founded"]) > 1:
            review.append(f"{m['id']}: {qid} ({how}) inception {year} vs {m['founded']} in catalog: "
                          "probably another entity -> skipped")
            m.pop("facts", None)
            continue
        if qid in used:
            review.append(f"{m['id']}: {qid} already used by {used[qid]} -> skipped")
            m.pop("facts", None)
            continue
        used[qid] = m["id"]
        m["facts"] = current[qid]
        if year and not m.get("founded"):
            m["founded"] = year
        elif year and m.get("founded") != year:
            review.append(f"{m['id']}: founded {m['founded']} in catalog, Wikidata says {year} (kept ours)")
        written += 1

    if not dry:
        # same byte layout as the hand-maintained file (LF, no trailing newline)
        MEDIA_FILE.write_bytes(json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))
    print(f"facts written for {written} outlets" + (" (dry run, nothing saved)" if dry else ""))
    for line in review:
        print("  review:", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
