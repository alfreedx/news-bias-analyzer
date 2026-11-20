"""Rule-based, public query expansion: equivalent words used by different outlets for the same thing.

The groups live in `data/synonyms.json` (versioned, reviewable). Everything here is deterministic and keeps the
search safe: the functions only produce *terms*; every one of them is double-quoted before it reaches FTS5 and
only word characters can appear inside the quotes.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from .config import settings
from .text import STOPWORDS, fold, stem

SYNONYMS_FILE = "synonyms.json"
MAX_SYNONYMS = 40  # equivalent terms added to one query, shared round-robin between its words
MAX_PHRASE_WORDS = 4
_TOKEN = re.compile(r"\w+", re.UNICODE)


def key_of(text: str) -> str:
    """Comparison key of a word or phrase: folded, each word with its plural removed."""
    return " ".join(stem(w) for w in _TOKEN.findall(fold(text)))


@dataclass(frozen=True)
class Synonyms:
    version: int
    updated: str
    groups: tuple[tuple[str, ...], ...]
    index: dict[str, tuple[int, ...]] = field(default_factory=dict, compare=False)

    def related(self, key: str) -> list[str]:
        """Other terms of every group holding `key`, in file order, without repeats."""
        seen = {key}
        out: list[str] = []
        for gi in self.index.get(key, ()):
            for term in self.groups[gi]:
                k = key_of(term)
                if k not in seen:
                    seen.add(k)
                    out.append(term)
        return out


def build(groups: list[list[str]], version: int = 1, updated: str = "") -> Synonyms:
    index: dict[str, list[int]] = {}
    for gi, group in enumerate(groups):
        for term in group:
            index.setdefault(key_of(term), []).append(gi)
    return Synonyms(version, updated, tuple(tuple(g) for g in groups), {k: tuple(v) for k, v in index.items()})


@lru_cache(maxsize=1)
def load(directory: Path | None = None) -> Synonyms:
    raw = json.loads(((directory or settings.catalog_dir) / SYNONYMS_FILE).read_text(encoding="utf-8"))
    groups: list[list[str]] = []
    for g in raw["groups"]:
        words = g["words"] if isinstance(g, dict) else g
        if len(words) < 2 or len({key_of(w) for w in words}) != len(words):
            raise ValueError(f"synonyms.json: group needs 2+ distinct terms: {words}")
        if any(fold(w) != w or not _TOKEN.findall(w) for w in words):
            raise ValueError(f"synonyms.json: terms must be folded (lowercase, no accents): {words}")
        groups.append(list(words))
    return build(groups, raw["version"], raw["updated"])


def meta() -> dict:
    s = load()
    return {"version": s.version, "updated": s.updated, "count": len(s.groups)}


@dataclass(frozen=True)
class Unit:
    """One thing the user asked for: a word, or several words that form a known phrase («partido popular»)."""

    literal: tuple[str, ...]  # folded words that must appear (prefix match for 4+ letters)
    display: str  # the words as typed (accents kept), for messages
    synonyms: tuple[str, ...] = ()  # folded alternative terms (a word or a phrase)


def parse_units(query: str, *, expand: bool = True, max_units: int = 8, syn: Synonyms | None = None) -> list[Unit]:
    """Split a query into units; each unit may carry equivalent terms (never when `expand` is false)."""
    syn = syn or load()
    raw: list[tuple[str, str]] = []  # (folded word, as typed)
    for original in _TOKEN.findall(unicodedata.normalize("NFC", query.lower())):
        raw.extend((piece, original) for piece in _TOKEN.findall(fold(original)))
    raw = [r for r in raw if len(r[0]) > 1]

    def keep(word: str) -> bool:
        return word not in STOPWORDS and not word.isdigit()

    keep_all = not any(keep(w) for w, _ in raw)
    units: list[Unit] = []
    seen: set[tuple[str, ...]] = set()
    i = 0
    while i < len(raw):
        n = 1
        if expand:
            for size in range(min(MAX_PHRASE_WORDS, len(raw) - i), 1, -1):
                if " ".join(stem(w) for w, _ in raw[i:i + size]) in syn.index:
                    n = size
                    break
        span = raw[i:i + n]
        words = tuple(w for w, _ in span if keep_all or keep(w))
        if words and words not in seen:
            seen.add(words)
            related = tuple(syn.related(" ".join(stem(w) for w, _ in span))) if expand else ()
            units.append(Unit(words, " ".join(o for _, o in span), related))
        i += n
    units = units[:max_units]
    return _cap(units) if expand else units


def _cap(units: list[Unit], limit: int = MAX_SYNONYMS) -> list[Unit]:
    """Keep at most `limit` equivalent terms in total, taken in turns from each word."""
    keep = [0] * len(units)
    left = limit
    while left > 0 and any(keep[i] < len(u.synonyms) for i, u in enumerate(units)):
        for i, u in enumerate(units):
            if keep[i] < len(u.synonyms) and left > 0:
                keep[i] += 1
                left -= 1
    return [replace(u, synonyms=u.synonyms[:k]) for u, k in zip(units, keep, strict=True)]


# --- FTS5 rendering: every term is double-quoted; only word characters and spaces appear inside the quotes ---
def literal_term(token: str) -> str:
    return f'"{stem(token)}"*' if len(token) >= 4 else f'"{token}"'


def literal_expr(unit: Unit) -> str:
    parts = [literal_term(t) for t in unit.literal]
    return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"


def forms(term: str) -> list[str]:
    """Spellings accepted for an equivalent term: itself plus its singular/plural (whole words, no prefixes)."""
    words = _TOKEN.findall(term)
    if len(words) != 1:
        return [" ".join(words)]
    w = words[0]
    out = [w]
    if len(w) >= 4:
        if w.endswith("s"):
            if stem(w) != w:
                out.append(stem(w))
        elif w.endswith("z"):
            out.append(w[:-1] + "ces")
        else:
            out.append(w + ("s" if w[-1] in "aeiou" else "es"))
    return out


def unit_expr(unit: Unit) -> str:
    lit = literal_expr(unit)
    if not unit.synonyms:
        return lit
    atoms = [f'"{f}"' for s in unit.synonyms for f in forms(s)]
    return "(" + " OR ".join([lit, *atoms]) + ")"


# --- matching on plain text (ranking, transparency, story-level checks) ------------------------------------
@dataclass(frozen=True)
class Text:
    words: frozenset[str]
    spaced: str  # " word word word " for phrase lookups


def analyse(text: str) -> Text:
    ws = _TOKEN.findall(fold(text))
    return Text(frozenset(ws), " " + " ".join(ws) + " ")


def has_literal(unit: Unit, t: Text) -> bool:
    for tok in unit.literal:
        if len(tok) >= 4:
            p = stem(tok)
            if not any(w.startswith(p) for w in t.words):
                return False
        elif tok not in t.words:
            return False
    return True


def has_term(term: str, t: Text) -> bool:
    fs = forms(term)
    if " " in fs[0]:
        return f" {fs[0]} " in t.spaced
    return any(f in t.words for f in fs)


def matched_synonym(unit: Unit, t: Text) -> str | None:
    return next((s for s in unit.synonyms if has_term(s, t)), None)
