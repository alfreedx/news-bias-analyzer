"""Verdict cues in a fact-checker's own headline.

Fact-checkers usually state their conclusion in the headline («Es falso que…», «No, …», «Bulo:»). We only
detect a small set of words so the page can say, attributed, «Maldita lo califica: «…»». The wording is always
the checker's; Contraste never issues a verdict of its own. Only the opening of the headline is read, so a
headline that merely mentions the word later on («Cómo se difunde un bulo») is not flagged.
"""

from __future__ import annotations

import re

from .text import fold

OPENING_CHARS = 40
_CUES = [
    (re.compile(r"\bfals[oa]s?\b"), "falso"),
    (re.compile(r"\bbulos?\b"), "bulo"),
    (re.compile(r"\benganos[oa]s?\b|\bengano\b"), "engañoso"),
    (re.compile(r"^no\s*[,:]"), "no"),
    (re.compile(r"\bno es (?:cierto|verdad)\b|\bes mentira\b|\bmentira\b"), "no es cierto"),
    (re.compile(r"\bsin pruebas\b|\bno hay pruebas\b|\bno existen pruebas\b"), "sin pruebas"),
    (re.compile(r"\bdescontextualizad[oa]s?\b|\bmanipulad[oa]s?\b"), "descontextualizado"),
    (re.compile(r"^si\s*,|\bes cierto\b|\bes verdad\b"), "cierto"),
]


def verdict_cue(title: str) -> str | None:
    """Name of the first verdict cue found in the opening of a fact-checker's headline, or None."""
    opening = fold(title or "").strip()[:OPENING_CHARS]
    for pattern, name in _CUES:
        if pattern.search(opening):
            return name
    return None
