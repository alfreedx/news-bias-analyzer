"""Text helpers: normalisation, and HTML stripping."""

from __future__ import annotations

import html
import re
import unicodedata

STOPWORDS = frozenset(
    """a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque bajo bien cada como con contra cual
    cuales cuando de del desde donde dos el ella ellas ello ellos en entre era eran es esa esas ese eso esos esta estan
    estas este esto estos fue fueron ha han hasta hay la las le les lo los mas me mi mientras muy nada ni no nos o os
    otra otras otro otros para pero poco por porque que quien quienes se segun ser si sin sobre son su sus tambien tan
    tanto te tiene tienen todo todos tras tu un una unas uno unos y ya yo ha he hemos va van sera seran puede pueden
    hace dice dicen asegura afirma tras tras ante nuevo nueva nuevos nuevas mas menos ano anos hoy ayer manana dia
    dias vez veces les sus cuyo cuya the of and in to els les per amb una uns des""".split()
)

_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"\w+", re.UNICODE)


def fold(text: str) -> str:
    """Lower-case and strip accents (used for matching, never for display)."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def clean_text(raw: str | None, max_len: int = 400) -> str:
    """Strip tags/entities from feed text. Output is plain text; templates escape it again."""
    if not raw:
        return ""
    text = html.unescape(_TAG.sub(" ", raw))
    text = _SPACES.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return text


def keywords(text: str) -> set[str]:
    """Meaningful words of a headline, used to group the same story across outlets."""
    return {w for w in _WORD.findall(fold(text)) if len(w) > 2 and w not in STOPWORDS and not w.isdigit()}
