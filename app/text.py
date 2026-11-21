"""Text helpers: normalisation, and HTML stripping."""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def stem(token: str) -> str:
    """Very light Spanish plural folding so «viviendas» also finds «vivienda»."""
    if len(token) >= 5 and token.endswith("s"):
        token = token[:-1]
        if len(token) >= 6 and token.endswith("e") and token[-2] in "nrlzd":
            token = token[:-1]
    return token


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


_TRACKING_EXACT = frozenset({"fbclid", "gclid", "igshid", "ref", "ref_src", "si", "ito", "cmpid"})


# --- URLs -------------------------------------------------------------------------
def safe_url(value) -> str | None:
    """The URL when it is a plain http(s) address of reasonable length, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return value


def normalize_url(url: str) -> str | None:
    """Canonical form used to compare addresses: https, lower-case host without "www.", no fragment, no trailing
    slash, no tracking parameters (utm_*, fbclid, ...); other query parameters kept, sorted. None when unusable."""
    url = safe_url(url)
    if url is None:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port not in (None, 80, 443):
        host = f"{host}:{parts.port}"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_EXACT)
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(query), ""))
