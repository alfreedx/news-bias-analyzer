"""What several independent outlets repeat about the same story, extracted with plain, documented rules.

No statistics beyond counting, no machine learning, no third-party NLP: regular expressions and set
operations over the headline and the RSS summary. Coincidence says that independent sources published
something, never that it is true.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from .analysis import Article, norm_title, owner, side_of
from .text import STOPWORDS, fold, keywords

MIN_VOICES = 3      # independent voices needed to talk about coincidences at all
MIN_SIDES = 2       # distinct labelled sides a coincident piece must span
MIN_GROUPS = 2      # distinct owners a coincident piece must span
MAX_PIECES = 8
MAX_ONE_SIDED = 5
MIN_ONE_SIDED_VOICES = 2
QUOTE_WORDS = (3, 15)

SIDE_LABEL = {"left": "izquierda", "center": "centro", "right": "derecha"}
SIDE_ORDER = ("left", "center", "right")
KIND_ORDER = {"cifra": 0, "cita": 1, "fecha": 2, "nombre": 3}

# --- Agencies --------------------------------------------------------------------------
# (regex over title + summary, name shown). First match wins.
AGENCIES = [
    (re.compile(r"\(\s*EFE\s*\)|\bag[eé]ncia\s+EFE\b|\bagencia\s+EFE\b", re.I), "EFE"),
    (re.compile(r"\(\s*EP\s*\)|\beuropa\s+press\b", re.I), "Europa Press"),
    (re.compile(r"\(\s*Servimedia\s*\)|\bservimedia\b", re.I), "Servimedia"),
    (re.compile(r"\bag[eé]ncias\b", re.I), "Agencias"),
]


def agency_of(article: Article) -> str | None:
    text = f"{article.title} {article.summary}"
    for pattern, name in AGENCIES:
        if pattern.search(text):
            return name
    return None


# --- Extractors ------------------------------------------------------------------------
# Each returns [(match key, display text)]; keys are accent/case-folded so that spellings coincide.

_MAG = r"(?:millones?|billones?|miles|mil|millardos?)"
_CUR = r"(?:euros?|d[oó]lares?|libras?|pesetas?)"
_UNITS = (
    r"euros?|d[oó]lares?|libras?|pesetas?|"
    r"muert[oa]s?|fallecid[oa]s?|herid[oa]s?|v[ií]ctimas?|desaparecid[oa]s?|detenid[oa]s?|"
    r"personas?|vecinos?|migrantes?|inmigrantes?|refugiad[oa]s?|menores|trabajadores?|"
    r"a[ñn]os?|meses|escaños?|votos?|diputad[oa]s?|puntos?|casos?|viviendas?|pisos?|plazas?|"
    r"toneladas?|kil[oó]metros?|km|hect[aá]reas?|metros?|vuelos?|pasajeros?|empleos?|contratos?"
)
_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?"
_NUMBER_RE = re.compile(
    rf"(?<![\w.,/-])(?P<num>{_NUM})\s?(?:(?P<pct>%|por\s+ciento)"
    rf"|(?P<mag>{_MAG})(?:\s+(?:de\s+)?(?P<mcur>{_CUR}))?(?!\w)"
    rf"|(?P<unit>{_UNITS})(?!\w))",
    re.I,
)
_SYMBOL_RE = re.compile(
    rf"(?P<sym>[€$])\s?(?P<num>{_NUM})(?:\s(?P<mag>{_MAG})(?!\w))?"
    rf"|(?<![\w.,/-])(?P<num2>{_NUM})\s?(?P<sym2>[€$])",
    re.I,
)

_CURRENCY = {"euro": "eur", "euros": "eur", "€": "eur", "libra": "gbp", "libras": "gbp",
             "dolar": "usd", "dolares": "usd", "$": "usd", "peseta": "pts", "pesetas": "pts"}

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
_DATE_RE = re.compile(
    rf"(?<![\w/-])(?P<d>[0-3]?\d)\s+de\s+(?P<m>{'|'.join(MONTHS)}|setiembre)(?:\s+de\s+(?P<y>20\d\d))?(?!\w)", re.I)
_DATE_TAG_RE = re.compile(r"(?<![\w/-])(?P<d>[0-3]?\d)-(?P<l>[A-ZÑ])(?![\w-])")


def norm_number(raw: str) -> str:
    """'1.500' -> '1500', '1,5' / '1.5' -> '1.5', '83,00' -> '83'."""
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", raw):
        raw = raw.replace(".", "")
    raw = raw.replace(",", ".")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw


def _unit_key(unit: str) -> str:
    u = re.sub(r"\s+", " ", fold(unit).strip())
    if u in _CURRENCY:
        return _CURRENCY[u]
    if u in ("km", "mil", "menores", "meses"):
        return u
    return u[:-1] if len(u) > 3 and u.endswith("s") else u


def extract_numbers(text: str) -> list[tuple[str, str]]:
    out = []
    for m in _NUMBER_RE.finditer(text):
        num = norm_number(m["num"])
        shown = " ".join(m.group(0).split())
        if m["pct"]:
            out.append((f"{num}|%", f"{m['num']} %"))
        elif m["mag"]:
            out.append((f"{num}|{_unit_key(m['mag'])}", shown))
        else:
            out.append((f"{num}|{_unit_key(m['unit'])}", shown))
    for m in _SYMBOL_RE.finditer(text):
        sym = m["sym"] or m["sym2"]
        raw = m["num"] or m["num2"]
        mag = m["mag"]
        key = f"{norm_number(raw)}|{_unit_key(mag) + '|' if mag else ''}{_CURRENCY[sym]}"
        out.append((key, " ".join(m.group(0).split())))
    return out


def extract_dates(text: str) -> list[tuple[str, str]]:
    out = []
    for m in _DATE_RE.finditer(text):
        month = fold(m["m"]).replace("setiembre", "septiembre")
        out.append((f"{int(m['d'])} {month}", f"{int(m['d'])} de {month}"))
    for m in _DATE_TAG_RE.finditer(text):
        out.append((f"{int(m['d'])}-{m['l'].lower()}", f"{int(m['d'])}-{m['l']}"))
    return out


_QUOTE_RE = re.compile(r"«([^«»]{5,300}?)»|“([^“”]{5,300}?)”|\"([^\"]{5,300}?)\"")
_WORD_RE = re.compile(r"\w+(?:[-']\w+)*", re.UNICODE)


def extract_quotes(text: str) -> list[tuple[str, str]]:
    out = []
    for m in _QUOTE_RE.finditer(text):
        inner = next(g for g in m.groups() if g is not None).strip()
        words = _WORD_RE.findall(inner)
        if QUOTE_WORDS[0] <= len(words) <= QUOTE_WORDS[1]:
            out.append((" ".join(fold(w) for w in words), f"«{inner.strip(' ,.;:')}»"))
    return out


_CONNECTORS = frozenset({"de", "del", "la", "las", "los", "y", "e"})
_SPLITTERS = frozenset({"y", "e"})
_NOT_NAME_START = STOPWORDS | _CONNECTORS
# Capitalised words that name nothing specific on their own.
GENERIC = frozenset(
    "gobierno estado ejecutivo espana espanol espanola espanoles pais paises ministerio ministro ministra "
    "presidente presidenta partido tribunal juez fiscal policia guardia civil hoy ayer segun tras video fotos "
    "foto directo ultima ultimas ultimo ultimos noticia noticias opinion entrevista analisis".split()
)
_TOKEN_RE = re.compile(r"(?P<w>\w+(?:[-']\w+)*)|(?P<p>[^\w\s])", re.UNICODE)
_BOUNDARY = frozenset(".!?:;¿¡—–|…")
MAX_NAME_WORDS = 4


def _is_cap(word: str) -> bool:
    return len(word) >= 2 and word[0].isupper() and not word.isdigit()


def _clean_run(run: list[str], at_sentence_start: bool) -> tuple[str, str] | None:
    """Turn a run of capitalised words and connectors into (key, display), or None when it is not a name."""
    while run and fold(run[-1]) in _CONNECTORS:
        run = run[:-1]
    lead = 0
    # a capitalised article only because it opens the sentence: «El Gobierno» -> «Gobierno»
    while at_sentence_start and lead < len(run) and fold(run[lead]) in _NOT_NAME_START:
        lead += 1
    run = run[lead:]
    while run and fold(run[0]) in _CONNECTORS:
        run = run[1:]
    caps = [w for w in run if _is_cap(w)]
    if not caps or len(caps) > MAX_NAME_WORDS:
        return None
    if at_sentence_start and len(caps) < 2:
        return None
    if all(fold(w) in GENERIC for w in caps):
        return None
    return " ".join(fold(w) for w in run), " ".join(run)


def extract_names(text: str) -> list[tuple[str, str]]:
    """Runs of 1-4 capitalised words (connectors allowed inside), never a lone word that starts a sentence.

    A run joined by «y»/«e» ("Illa y Puigdemont") yields the whole run and each part.
    """
    tokens = [(m["w"], m["p"]) for m in _TOKEN_RE.finditer(text)]
    words = [w for w, _ in tokens if w and any(c.isalpha() for c in w)]
    if words and sum(w.isupper() and len(w) > 3 for w in words) / len(words) > 0.5:
        return []  # headline in capitals: capitalisation says nothing
    out: list[tuple[str, str]] = []
    state = {"run": [], "start": True}
    sentence_start = True

    def flush() -> None:
        run = state["run"]
        state["run"] = []
        if not run:
            return
        pieces = [_clean_run(run, state["start"])]
        if any(fold(w) in _SPLITTERS for w in run):
            part: list[str] = []
            parts = []
            for w in [*run, "y"]:
                if fold(w) in _SPLITTERS:
                    parts.append(part)
                    part = []
                else:
                    part.append(w)
            pieces += [_clean_run(p, state["start"] and i == 0) for i, p in enumerate(parts) if p]
        out.extend(p for p in pieces if p)

    for word, punct in tokens:
        if word is None:
            flush()
            if punct in _BOUNDARY:
                sentence_start = True
            continue
        if _is_cap(word):
            if not state["run"]:
                state["start"] = sentence_start
            state["run"].append(word)
        elif state["run"] and fold(word) in _CONNECTORS:
            state["run"].append(word)
        else:
            flush()
        sentence_start = False
    flush()
    return out


EXTRACTORS = (("cifra", extract_numbers), ("cita", extract_quotes), ("fecha", extract_dates),
              ("nombre", extract_names))


def extract_pieces(text: str) -> list[tuple[str, str, str]]:
    """All claim pieces of a text as (kind, key, display)."""
    return [(kind, key, shown) for kind, fn in EXTRACTORS for key, shown in fn(text)]


# --- Voices ----------------------------------------------------------------------------
@dataclass
class Voice:
    """One independent source: an owner (group, publisher or outlet), or one news agency's single text."""

    key: str
    label: str
    agency: bool
    articles: list[Article] = field(default_factory=list)

    @property
    def outlets(self) -> list[str]:
        return sorted({a.source_name for a in self.articles}, key=str.lower)

    @property
    def side(self) -> str | None:
        """Side of the voice's labelled outlets (the most frequent one; None when none is labelled).

        An agency text is not an outlet's line: it carries no side.
        """
        if self.agency:
            return None
        counts = Counter(s for a in self.articles if (s := side_of(a.position)))
        return counts.most_common(1)[0][0] if counts else None


def build_voices(articles: list[Article]) -> list[Voice]:
    """Independent voices: copies of one headline merged, one voice per owner, one per agency text."""
    unique: dict[str, Article] = {}
    for a in articles:
        unique.setdefault(norm_title(a.title) or a.url, a)
    voices: dict[str, Voice] = {}
    for a in unique.values():
        agency = agency_of(a)
        key, label = (f"agencia:{agency}", f"Agencia {agency}") if agency else (f"owner:{owner(a)}", owner(a))
        voices.setdefault(key, Voice(key, label, agency is not None)).articles.append(a)
    return list(voices.values())


def common_agencies(articles: list[Article]) -> list[tuple[str, int]]:
    """Agencies whose text was carried by two or more outlets: (name, number of outlets)."""
    outlets: dict[str, set[str]] = {}
    for a in articles:
        if (name := agency_of(a)):
            outlets.setdefault(name, set()).add(a.source_name)
    return sorted(((n, len(o)) for n, o in outlets.items() if len(o) >= 2), key=lambda t: -t[1])


# --- Consensus -------------------------------------------------------------------------
@dataclass
class Piece:
    kind: str
    key: str
    text: str
    voices: int
    sides: list[str]
    outlets: list[str]

    @property
    def sides_label(self) -> str:
        return ", ".join(SIDE_LABEL[s] for s in self.sides)


@dataclass
class OneSided:
    side: str
    piece: Piece

    @property
    def side_label(self) -> str:
        return SIDE_LABEL[self.side]


@dataclass
class Consensus:
    voices: int
    enough: bool
    pieces: list[Piece]
    one_sided: list[OneSided]
    headline: Article | None
    agencies: list[tuple[str, int]]


def _piece_table(voices: list[Voice]) -> tuple[dict[tuple[str, str], set[int]], dict[tuple[str, str], Counter]]:
    holders: dict[tuple[str, str], set[int]] = {}
    spellings: dict[tuple[str, str], Counter] = {}
    for i, v in enumerate(voices):
        seen: set[tuple[str, str]] = set()
        for a in v.articles:
            text = f"{a.title}. {a.summary}" if a.summary else a.title
            for kind, key, shown in extract_pieces(text):
                k = (kind, key)
                holders.setdefault(k, set()).add(i)
                if k not in seen:
                    spellings.setdefault(k, Counter())[shown] += 1
                    seen.add(k)
    return holders, spellings


def representative_headline(voices: list[Voice]) -> Article | None:
    """The voice headline sharing the most keywords, on average, with the other voices' headlines."""
    if not voices:
        return None
    if len(voices) == 1:
        return voices[0].articles[0]
    heads = [v.articles[0] for v in voices]
    keys = [keywords(h.title) for h in heads]

    def avg(i: int) -> float:
        total = 0.0
        for j, other in enumerate(keys):
            if j != i and keys[i] and other:
                total += len(keys[i] & other) / min(len(keys[i]), len(other))
        return total / (len(keys) - 1)

    best = max(range(len(heads)), key=lambda i: (avg(i), -i))
    return heads[best]


def _drop_contained(pieces: list[Piece]) -> list[Piece]:
    """Drop a name that is just part of a longer name backed by as many voices ('Sánchez' vs 'Pedro Sánchez')."""
    kept = []
    for p in pieces:
        words = f" {p.key} "
        if p.kind == "nombre" and any(q.kind == "nombre" and q.voices >= p.voices and len(q.key) > len(p.key)
                                      and words in f" {q.key} " for q in pieces):
            continue
        kept.append(p)
    return kept


def _make_piece(kind: str, key: str, idx: set[int], voices: list[Voice], spellings: dict,
                side_of_voice: list) -> Piece:
    text = spellings[(kind, key)].most_common(1)[0][0]
    sides = [s for s in SIDE_ORDER if any(side_of_voice[i] == s for i in idx)]
    outlets = sorted({o for i in idx for o in voices[i].outlets}, key=str.lower)
    return Piece(kind, key, text, len(idx), sides, outlets)


def _sort_key(p: Piece):
    return (-p.voices, KIND_ORDER[p.kind], -len(p.text), p.key)


@lru_cache(maxsize=1)
def _outlet_names() -> frozenset[str]:
    from .catalog import get_catalog
    names = set()
    for m in get_catalog().media.values():
        names.add(" ".join(fold(m.name).split()))
    return frozenset(names)


def consensus(articles: list[Article], *, min_voices: int = MIN_VOICES, min_sides: int = MIN_SIDES,
              min_groups: int = MIN_GROUPS) -> Consensus:
    voices = build_voices(articles)
    agencies = common_agencies(articles)
    headline = representative_headline(voices)
    if len(voices) < min_voices:
        return Consensus(len(voices), False, [], [], headline, agencies)

    holders, spellings = _piece_table(voices)
    # A newspaper quoting another («según El Mundo») is not a fact about the story.
    for k in [k for k in holders if k[0] == "nombre" and k[1] in _outlet_names()]:
        del holders[k]
    side_of_voice = [v.side for v in voices]
    covered = {s for s in side_of_voice if s}

    pieces: list[Piece] = []
    for (kind, key), idx in holders.items():
        sides = {side_of_voice[i] for i in idx} - {None}
        groups = {voices[i].key for i in idx}
        if len(idx) >= min_voices and len(groups) >= min_groups and len(sides) >= min_sides:
            pieces.append(_make_piece(kind, key, idx, voices, spellings, side_of_voice))
    pieces.sort(key=_sort_key)
    pieces = _drop_contained(pieces)[:MAX_PIECES]

    shown = {(p.kind, p.key) for p in pieces}
    candidates: list[OneSided] = []
    for (kind, key), idx in holders.items():
        if (kind, key) in shown or len(idx) < MIN_ONE_SIDED_VOICES:
            continue
        sides = {side_of_voice[i] for i in idx}
        if None in sides or len(sides) != 1:
            continue
        side = next(iter(sides))
        opposite = {"left": "right", "right": "left"}.get(side)
        if opposite and opposite in covered:
            candidates.append(OneSided(side, _make_piece(kind, key, idx, voices, spellings, side_of_voice)))
    candidates.sort(key=lambda o: _sort_key(o.piece))
    keep = {id(p) for p in _drop_contained([o.piece for o in candidates])}
    one_sided = [o for o in candidates if id(o.piece) in keep][:MAX_ONE_SIDED]
    return Consensus(len(voices), True, pieces, one_sided, headline, agencies)
