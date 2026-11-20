"""Section evidence read from what the outlet itself publishes: RSS categories and URL directories.

Two small, curated lists (documented in /metodologia):

* OPINION: opinion pieces (opinión, tribuna, editorial, columna, columnistas, blogs, firmas, cartas al director).
  «Análisis» is deliberately NOT included: many outlets use it for news analysis, not opinion.
* INCIDENT: crime and accident sections (sucesos, policía, tribunales, incendios, tráfico…). A match on one of
  these only lowers an item's rank in searches whose query is not about such events («incendio en una
  vivienda» when searching «vivienda»); the item is never hidden.

Matching is by whole words, without accents or capitals, and only on the outlet's own labels: a category or a
directory of the URL path (the last segment, the slug of the piece, is ignored). We never judge the text.
"""

from __future__ import annotations

from .sponsored import _contains_phrase, _path_segments, _tokens, split_categories
from .text import fold

OPINION_TERMS = ("opinion", "tribuna", "editorial", "columna", "columnas", "columnista", "columnistas", "blog",
                 "blogs", "firmas", "cartas al director", "cartas de los lectores")
INCIDENT_TERMS = ("sucesos", "suceso", "policia", "policial", "tribunales", "incendios", "incendio", "trafico",
                  "accidentes", "crimen", "crimenes", "seguridad ciudadana")
# Words of a search query that say the reader is looking for exactly this kind of event.
INCIDENT_QUERY_WORDS = frozenset(
    "suceso sucesos policia policial tribunal tribunales juicio juicios incendio incendios trafico accidente "
    "accidentes crimen crimenes asesinato asesinatos detenido detenidos detenida detenidas homicidio robo robos "
    "agresion agresiones muerto muertos fallecido fallecidos herido heridos".split()
)


class _Needles:
    """Tokenised once: single words are checked with a set intersection, phrases only when all their words occur."""

    def __init__(self, terms: tuple[str, ...]):
        token_lists = [_tokens(t) for t in terms]
        self.words = frozenset(t[0] for t in token_lists if len(t) == 1)
        self.phrases = [t for t in token_lists if len(t) > 1]

    def hit(self, tokens: list[str]) -> bool:
        have = set(tokens)
        if self.words & have:
            return True
        return any(have.issuperset(p) and _contains_phrase(tokens, p) for p in self.phrases)


_OPINION = _Needles(OPINION_TERMS)
_INCIDENT = _Needles(INCIDENT_TERMS)


def _evidence(needles: _Needles, url: str, categories: str | None) -> str | None:
    for cat in split_categories(categories):
        if needles.hit(_tokens(cat)):
            return f"Categoría RSS «{cat}»"
    for seg in _path_segments(url):
        if needles.hit(_tokens(seg)):
            return f"Ruta /{seg}/"
    return None


def opinion_evidence(url: str, categories: str | None) -> str | None:
    """What the outlet published that marks the piece as opinion (None when nothing does)."""
    return _evidence(_OPINION, url, categories)


def incident_evidence(url: str, categories: str | None) -> str | None:
    """What the outlet published that files the piece under crime, accidents or courts."""
    return _evidence(_INCIDENT, url, categories)


def query_is_incident(query: str) -> bool:
    """True when the query itself contains a word about such events (no demotion then)."""
    return any(w in INCIDENT_QUERY_WORDS or w.rstrip("s") in INCIDENT_QUERY_WORDS for w in _tokens(fold(query)))
