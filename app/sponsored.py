"""Sponsored content, ONLY as marked by the outlet itself.

Contraste never judges whether a piece is advertising. It reflects a mark the outlet put on it: an RSS
category («Contenido patrocinado»), a URL section (/branded/) or a label at the start of the headline
(«Patrocinado:»). The rules are public, versioned data in `data/sponsored_rules.json`; each one says which
field it reads and how the outlet marks the piece. A piece without a mark is shown exactly like any other:
not finding a mark does NOT mean the piece is not advertising.

Matching is on whole words, accent- and case-insensitive: the category «Salud pública» or the section
«valencia-bc» never match «publicidad» or «bc». Rules with a `media_id` apply only to that outlet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import settings
from .text import fold

RULES_FILE = "sponsored_rules.json"
FIELDS = ("category", "path", "title_prefix")
CATEGORY_SEP = " | "
_NON_WORD = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class Rule:
    id: str
    field: str
    pattern: str
    label: str
    media_id: str | None = None


def _tokens(text: str) -> list[str]:
    return _NON_WORD.sub(" ", fold(text)).split()


def _contains_phrase(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


@lru_cache(maxsize=1)
def load_rules(directory: Path | None = None) -> tuple[Rule, ...]:
    raw = json.loads(((directory or settings.catalog_dir) / RULES_FILE).read_text(encoding="utf-8"))
    rules = tuple(Rule(r["id"], r["field"], r["pattern"], r["label"], r.get("media_id")) for r in raw["rules"])
    if len({r.id for r in rules}) != len(rules):
        raise ValueError("sponsored_rules.json: duplicate rule id")
    for r in rules:
        if r.field not in FIELDS:
            raise ValueError(f"sponsored_rules.json: unknown field {r.field!r} in {r.id}")
    return rules


def rules_by_id() -> dict[str, Rule]:
    return {r.id: r for r in load_rules()}


def rules_meta() -> dict:
    raw = json.loads((settings.catalog_dir / RULES_FILE).read_text(encoding="utf-8"))
    return {"version": raw["version"], "updated": raw["updated"], "count": len(raw["rules"])}


def split_categories(stored: str | None) -> list[str]:
    return [c for c in (stored or "").split(CATEGORY_SEP) if c]


def _path_segments(url: str) -> list[str]:
    """Directory segments of the URL path; the last one is the slug of the piece itself and is ignored."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return []
    return [unquote(s) for s in path.split("/") if s][:-1]


def _evidence_for(rule: Rule, url: str, title: str, categories: list[str]) -> str | None:
    """What the outlet published that triggers this rule (None when it does not)."""
    needle = _tokens(rule.pattern)
    if rule.field == "category":
        for cat in categories:
            if _contains_phrase(_tokens(cat), needle):
                return f"Categoría RSS «{cat}»"
    elif rule.field == "path":
        for seg in _path_segments(url):
            if _contains_phrase(_tokens(seg), needle):
                return f"Ruta /{seg}/"
    elif rule.field == "title_prefix" and fold(title).lstrip().startswith(fold(rule.pattern)):
        return f"Titular que empieza por «{rule.pattern}»"
    return None


def match(media_id: str | None, url: str, title: str, categories: str | list[str] | None = None) -> str | None:
    """Id of the first rule that matches, or None. `categories` is the stored string or a list."""
    cats = split_categories(categories) if isinstance(categories, str) or categories is None else list(categories)
    for rule in load_rules():
        if rule.media_id and rule.media_id != media_id:
            continue
        if _evidence_for(rule, url, title, cats):
            return rule.id
    return None


def evidence(rule_id: str | None, url: str, title: str, categories: str | None) -> str | None:
    """Exact evidence text for the badge's title attribute, recomputed from what is stored."""
    rule = rules_by_id().get(rule_id) if rule_id else None
    if rule is None:
        return None
    return _evidence_for(rule, url, title, split_categories(categories)) or rule.label
