"""Coverage analysis: who reports a topic, from which side, owned by whom."""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cached_property

from . import sections, sponsored
from .catalog import Catalog, Media
from .facts import language_note
from .scoring import POSITION_LABELS
from .text import _WORD, fold, keywords

POSITIONS = [-2, -1, 0, 1, 2]

SIDES = [("left", "Izquierda"), ("center", "Centro"), ("right", "Derecha")]


# --- Agencies --------------------------------------------------------------------------
# (regex over title + summary, name shown). First match wins.
AGENCIES = [
    (re.compile(r"\(\s*EFE\s*\)|\bag[eé]ncia\s+EFE\b|\bagencia\s+EFE\b", re.I), "EFE"),
    (re.compile(r"\(\s*EP\s*\)|\beuropa\s+press\b", re.I), "Europa Press"),
    (re.compile(r"\(\s*Servimedia\s*\)|\bservimedia\b", re.I), "Servimedia"),
    (re.compile(r"\bag[eé]ncias\b", re.I), "Agencias"),
]


def agency_text_of(title: str, summary: str) -> str | None:
    text = f"{title} {summary}"
    for pattern, name in AGENCIES:
        if pattern.search(text):
            return name
    return None


def side_of(position: int | None) -> str | None:
    """Three-way bucket of an assessed position (None when the outlet has no published label)."""
    if position is None:
        return None
    return "left" if position < 0 else "right" if position > 0 else "center"


def norm_title(title: str) -> str:
    """Accent-, case- and whitespace-insensitive form of a headline, used to spot syndicated copies."""
    return " ".join(fold(title).split())


@dataclass
class Article:
    title: str
    url: str
    summary: str
    published: int
    media: Media | None
    source_name: str
    relevance: float = 0.0
    keys: set[str] = field(default_factory=set)
    author: str | None = None  # byline as published; nothing else is known or inferred about the person
    id: int | None = None  # row id in the local index (None for live Google News results)
    sponsored: str | None = None  # id of the rule (data/sponsored_rules.json) matching a mark the outlet itself put
    categories: str | None = None  # the outlet's own RSS category labels, joined

    @property
    def sponsored_evidence(self) -> str | None:
        """Exact mark the outlet published («Categoría RSS «Contenido patrocinado»», «Ruta /branded/»…)."""
        return sponsored.evidence(self.sponsored, self.url, self.title, self.categories) if self.sponsored else None

    @cached_property
    def opinion_evidence(self) -> str | None:
        """What the outlet published that marks this piece as opinion (RSS category or URL section)."""
        return sections.opinion_evidence(self.url, self.categories)

    @property
    def opinion(self) -> bool:
        return self.opinion_evidence is not None

    @cached_property
    def incident_evidence(self) -> str | None:
        """What the outlet published that files this piece under sucesos, tribunales, tráfico…"""
        return sections.incident_evidence(self.url, self.categories)

    @property
    def position(self) -> int | None:
        return self.media.assessment.position if self.media else None

    @property
    def subtitle(self) -> str:
        """The RSS summary when it adds something to the headline (not empty, not a repeat of it)."""
        text, title = norm_title(self.summary or ""), norm_title(self.title)
        if not text or text == title or text.startswith(title):
            return ""
        return self.summary

    @property
    def lang_note(self) -> str | None:
        """«en catalán» etc. when the outlet's languages (Wikidata) are minority ones and not Spanish."""
        return language_note(self.media.derived.languages) if self.media else None

    def to_public(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "published": self.published,
            "source": self.source_name,
            "author": self.author,
            "media": self.media.to_public() if self.media else None,
            **({"sponsored_mark": self.sponsored_evidence} if self.sponsored else {}),
        }


@dataclass
class Row:
    """One visible line of a story: a headline, possibly carried verbatim by several outlets."""

    articles: list[Article]

    @property
    def lead(self) -> Article:
        return self.articles[0]

    @property
    def position(self) -> int | None:
        return self.lead.position

    @property
    def published(self) -> int:
        return max(a.published for a in self.articles)

    @property
    def subtitle(self) -> str:
        return self.lead.subtitle

    @property
    def first(self) -> int:
        """Earliest publication time among the copies (as given by each outlet's RSS)."""
        return min(a.published for a in self.articles)

    @property
    def lang_note(self) -> str | None:
        return self.lead.lang_note

    @property
    def author(self) -> str | None:
        """The byline when every copy of the headline carries the same one."""
        names = {a.author for a in self.articles}
        return self.lead.author if len(names) == 1 else None

    @property
    def group(self):
        """The owner group when every outlet of the row shares it (a fact about ownership, nothing more)."""
        groups = {a.media.group.id if a.media and a.media.group else None for a in self.articles}
        if len(groups) == 1 and None not in groups:
            return self.lead.media.group
        return None


def dedupe(articles: list[Article]) -> list[Row]:
    """Merge items with an identical normalised title into one row, keeping first-seen order."""
    rows: dict[str, Row] = {}
    for a in articles:
        key = norm_title(a.title) or a.url
        if key in rows:
            rows[key].articles.append(a)
        else:
            rows[key] = Row([a])
    return list(rows.values())


@dataclass
class Story:
    """The same news event as told by one or more outlets."""

    articles: list[Article]
    score: float = 0.0
    seed: Article | None = None  # the most relevant headline, kept as the story's title
    story_id: int | None = None  # id of the persisted story this cluster maps to, when there is one
    incidental: bool = False  # most headlines come from sucesos/tribunales sections and the search was not about that

    @property
    def lead(self) -> Article:
        return self.seed or self.articles[0]

    @property
    def outlets(self) -> int:
        return len({a.source_name for a in self.articles})

    @property
    def rows(self) -> list[Row]:
        return dedupe(self.articles)

    @property
    def timeline(self) -> list[Row]:
        """Deduplicated rows ordered by first publication time (oldest first)."""
        return sorted(self.rows, key=lambda r: (r.first, r.lead.source_name))

    @property
    def groups(self) -> int:
        """Distinct owners (group, else publisher, else the outlet itself)."""
        return len({_owner(a) for a in self.articles})

    @property
    def side_counts(self) -> dict[str, int]:
        counts = {"left": 0, "center": 0, "right": 0}
        for a in self.articles:
            side = side_of(a.position)
            if side:
                counts[side] += 1
        return counts

    @property
    def comparison(self) -> list[tuple[str, str, Article | None]] | None:
        """One representative headline per side, or None unless at least two sides cover the story."""
        best: dict[str, Article] = {}
        for a in self.articles:
            side = side_of(a.position)
            if side and (side not in best or (a.relevance, a.published) > (best[side].relevance, best[side].published)):
                best[side] = a
        if len(best) < 2:
            return None
        return [(key, label, best.get(key)) for key, label in SIDES]

    @property
    def topic_query(self) -> str:
        """Words shared by most headlines of the story, in their original spelling."""
        counts = Counter(k for a in self.articles for k in a.keys)
        needed = max(1, len(self.articles) // 2)
        chosen = [k for k, n in counts.most_common() if n >= needed][:4] or list(self.lead.keys)[:4]
        originals: dict[str, str] = {}
        for a in self.articles:
            for word in _WORD.findall(a.title):
                originals.setdefault(fold(word), word.lower())
        return " ".join(originals.get(k, k) for k in chosen)

    @property
    def positions(self) -> list[int]:
        return sorted({a.position for a in self.articles if a.position is not None})

    @property
    def spread_label(self) -> str:
        pos = self.positions
        if not pos:
            return "Solo medios sin etiqueta publicada"
        if pos[0] < 0 < pos[-1]:
            return "La publican medios de izquierda y de derecha"
        if pos[-1] < 0:
            return "Solo la publican medios de izquierda"
        if pos[0] > 0:
            return "Solo la publican medios de derecha"
        return "La publican medios de centro y de un solo lado"


def owner(a: Article) -> str:
    """Owner key of an article: group, else publisher, else the outlet itself."""
    return _owner(a)


def _owner(a: Article) -> str:
    if a.media and a.media.group:
        return a.media.group.name
    return (a.media.publisher if a.media and a.media.publisher else a.source_name)


def story_score(articles: list[Article]) -> float:
    """Rank stories by diversity of coverage, not just size.

    base      = best relevance of the story (1.0 when headlines were not ranked, e.g. front page)
    score     = base * (1 + 0.25 * (outlets - 1) + 0.4 * (owner groups - 1) + 0.4 * (labelled sides - 1))
    penalties = x0.5 when every item is a syndicated copy of one headline,
                x0.5 when no outlet has a published label ("sin etiqueta").
    "Sides" are the left / centre / right buckets of the external assessments only.
    """
    base = max((a.relevance for a in articles), default=0.0)
    if base <= 0:
        base = 1.0
    outlets = len({a.source_name for a in articles})
    groups = len({_owner(a) for a in articles})
    sides = len({side_of(a.position) for a in articles} - {None})
    score = base * (1 + 0.25 * (outlets - 1) + 0.4 * (groups - 1) + 0.4 * max(0, sides - 1))
    if len(articles) > 1 and len({norm_title(a.title) for a in articles}) == 1:
        score *= 0.5
    if sides == 0:
        score *= 0.5
    return score


def blind_spots(stories: list[Story], limit: int = 4, min_labelled: int = 3, threshold: float = 0.8) -> list[dict]:
    """Stories where at least `min_labelled` labelled outlets report and >= `threshold` of them sit on one side."""
    found = []
    for s in stories:
        counts = s.side_counts
        labelled = sum(counts.values())
        if labelled < min_labelled:
            continue
        for side in ("left", "right"):
            if counts[side] / labelled >= threshold:
                found.append({"story": s, "side": side, "side_outlets": counts[side], "labelled": labelled})
    found.sort(key=lambda b: (b["labelled"], b["story"].score), reverse=True)
    return found[:limit]


MIN_RATED = 30  # labelled headlines needed before any percentage or coloured bar is shown
PUBLISH_NOTE = "El reparto refleja cuánto publica cada medio; no mide el sesgo de la noticia."


def coverage_summary(cov: Coverage) -> str:
    """Plain-language, purely descriptive reading of the coverage counts, with its base stated explicitly.

    Percentages appear only with a solid base (`Coverage.reliable`: 30+ labelled headlines and at least half
    of the total). Otherwise only counts are given, with an explicit warning. Either way the text says that the
    split reflects how much each outlet publishes.
    """
    unrated = cov.total - cov.rated
    if not cov.rated:
        return f"Ningún titular procede de medios con etiqueta publicada. {PUBLISH_NOTE}"
    left = cov.by_position[-2] + cov.by_position[-1]
    right = cov.by_position[1] + cov.by_position[2]
    centre = cov.by_position[0]
    if not cov.reliable and not unrated:
        return (f"Poca base para comparar: solo hay {cov.rated} titulares, todos de medios con etiqueta publicada. "
                "El reparto refleja también cuánto publica cada medio.")
    if not cov.reliable:
        return (f"Poca base para comparar: solo {cov.rated} de {cov.total} titulares proceden de medios con "
                "etiqueta publicada. El reparto refleja también cuánto publica cada medio.")
    pct_ = lambda n: round(100 * n / cov.rated)  # noqa: E731
    bits = []
    for n, text in ((right, "a la derecha del centro"), (left, "a la izquierda"), (centre, "en el centro")):
        if n:
            bits.append((n, f"el {pct_(n)} % procede de medios situados {text}"))
    bits.sort(key=lambda b: -b[0])
    texts = [b[1] for b in bits]
    for i in range(1, len(texts)):
        texts[i] = texts[i].replace("procede de medios situados", "de medios situados", 1)
    body = texts[0] if len(texts) == 1 else ", ".join(texts[:-1]) + " y " + texts[-1]
    parts = [f"De los {cov.rated} titulares de medios con etiqueta, {body}."]
    if unrated:
        parts.append(f"Calculado sobre {cov.rated} de {cov.total} titulares "
                     "(el resto son de medios sin etiqueta publicada).")
    parts.append(PUBLISH_NOTE)
    return " ".join(parts)


@dataclass
class Coverage:
    total: int
    outlets: int
    rated: int
    by_position: dict[int, int]
    by_group: list[tuple[str, int]]
    by_media: list[tuple[Media | None, str, int]]
    left_share: float
    center_share: float
    right_share: float
    warnings: list[dict]
    agency_headlines: int = 0  # headlines that carry a news agency's mark (EFE, Europa Press…)
    agency_names: list[str] = field(default_factory=list)

    @property
    def reliable(self) -> bool:
        """Enough labelled headlines to quote shares: at least MIN_RATED of them and at least half of the total."""
        return self.rated >= MIN_RATED and self.rated * 2 >= self.total

    def to_public(self) -> dict:
        return {
            "total": self.total,
            "outlets": self.outlets,
            "rated": self.rated,
            "by_position": {POSITION_LABELS[k]: v for k, v in self.by_position.items()},
            "by_group": self.by_group,
            "shares": {"left": self.left_share, "center": self.center_share, "right": self.right_share},
            "warnings": self.warnings,
            "agency_headlines": self.agency_headlines,
        }


def make_article(catalog: Catalog, *, title: str, url: str, summary: str, published: int,
                 media_id: str | None = None, source_url: str | None = None,
                 source_name: str | None = None, author: str | None = None,
                 article_id: int | None = None, sponsored: str | None = None,
                 categories: str | None = None) -> Article:
    media = catalog.media.get(media_id) if media_id else None
    if media is None and source_url:
        media = catalog.by_url(source_url)
    if media is None:
        media = catalog.by_url(url)
    return Article(
        title=title,
        url=url,
        summary=summary,
        published=published,
        media=media,
        source_name=media.name if media else (source_name or "Medio desconocido"),
        keys=keywords(title),
        author=author,
        id=article_id,
        sponsored=sponsored,
        categories=categories,
    )


def article_from_row(catalog: Catalog, r) -> Article:
    """Build an Article from a row of the local index."""
    return make_article(catalog, title=r["title"], url=r["url"], summary=r["summary"], published=r["published"],
                        media_id=r["media_id"], author=r["author"], article_id=r["id"],
                        sponsored=r["sponsored"], categories=r["categories"])


def split_sponsored(articles: list[Article]) -> tuple[list[Article], list[Article]]:
    """(regular, marked as sponsored by their outlet). Marked pieces never enter clustering or counts."""
    return [a for a in articles if not a.sponsored], [a for a in articles if a.sponsored]


def rank(articles: list[Article], bm25: dict[str, float] | None = None, now: int | None = None) -> None:
    """Relevance = text match blended with freshness (half-life: 3 days)."""
    now = now or int(time.time())
    for a in articles:
        text_score = -(bm25 or {}).get(a.url, -1.0)  # bm25() is negative: lower is better
        age_days = max(0.0, (now - a.published) / 86400)
        a.relevance = text_score * (0.5 + 0.5 * math.exp(-age_days / 4.3))
    articles.sort(key=lambda a: a.relevance, reverse=True)


MIN_SHARED, MIN_OVERLAP = 3, 0.5


def keyword_overlap(shared: int, n_a: int, n_b: int, min_shared: int = MIN_SHARED,
                    min_overlap: float = MIN_OVERLAP) -> float:
    """The one similarity rule of the project: overlap coefficient of two keyword sets, given how many words
    they share. Returns 0.0 when the headlines are not about the same event."""
    if min(n_a, n_b) < 1 or shared < min(min_shared, n_a):
        return 0.0
    overlap = shared / min(n_a, n_b)
    return overlap if overlap >= min_overlap else 0.0


def best_match(keys: set[str], seeds: list[set[str]], index: dict[str, list[int]],
               min_shared: int = MIN_SHARED, min_overlap: float = MIN_OVERLAP) -> int | None:
    """Index of the seed (via the inverted `index` word -> seed positions) that best matches `keys`."""
    if len(keys) < 2:
        return None
    best, best_score = None, 0.0
    candidates = Counter(i for k in keys for i in index.get(k, ()))
    for idx, shared in candidates.items():
        overlap = keyword_overlap(shared, len(keys), len(seeds[idx]), min_shared, min_overlap)
        if overlap > best_score:
            best, best_score = idx, overlap
    return best


def cluster(articles: list[Article], min_shared: int = MIN_SHARED, min_overlap: float = MIN_OVERLAP) -> list[Story]:
    """Group headlines about the same event (greedy, inverted index, overlap coefficient)."""
    articles = [a for a in articles if not a.sponsored]
    stories: list[Story] = []
    seeds: list[set[str]] = []
    index: dict[str, list[int]] = defaultdict(list)
    for art in articles:
        best = best_match(art.keys, seeds, index, min_shared, min_overlap)
        if best is None:
            stories.append(Story([art], seed=art))
            seeds.append(art.keys)
            for k in art.keys:
                index[k].append(len(stories) - 1)
        else:
            story = stories[best]
            # one headline per outlet per story keeps comparisons fair
            if all(a.source_name != art.source_name for a in story.articles):
                story.articles.append(art)
    for s in stories:
        if s.seed is not None and s.seed.opinion:  # an opinion column never gives its name to a news story
            s.seed = next((a for a in s.articles if not a.opinion), s.seed)
        s.score = story_score(s.articles)
        s.articles.sort(key=lambda a: (a.position is None, a.position if a.position is not None else 0))
    stories.sort(key=lambda s: s.score, reverse=True)
    return stories


def coverage(articles: list[Article]) -> Coverage:
    articles = [a for a in articles if not a.sponsored]
    by_position = {p: 0 for p in POSITIONS}
    groups: Counter[str] = Counter()
    media_counts: Counter[str] = Counter()
    media_ref: dict[str, Media | None] = {}
    agencies: Counter[str] = Counter(n for a in articles if (n := agency_text_of(a.title, a.summary)))
    for a in articles:
        if a.position is not None:
            by_position[a.position] += 1
        if a.media and a.media.group:
            groups[a.media.group.name] += 1
        elif a.media:
            groups[a.media.publisher or f"{a.media.name} (sin grupo)"] += 1
        else:
            groups["Sin identificar"] += 1
        media_counts[a.source_name] += 1
        media_ref[a.source_name] = a.media

    rated = sum(by_position.values())
    left = by_position[-2] + by_position[-1]
    right = by_position[1] + by_position[2]
    center = by_position[0]
    share = (lambda n: round(n / rated, 3)) if rated else (lambda n: 0.0)

    warnings: list[dict] = []
    if rated >= 6:
        if share(left) <= 0.1 < share(right):
            warnings.append({"kind": "blindspot", "title": "Punto ciego de la izquierda",
                             "text": "Casi toda la cobertura viene de medios de centroderecha o derecha. "
                                     "Puede que el tema esté infrarrepresentado (o enfocado distinto) "
                                     "en la izquierda."})
        if share(right) <= 0.1 < share(left):
            warnings.append({"kind": "blindspot", "title": "Punto ciego de la derecha",
                             "text": "Casi toda la cobertura viene de medios de centroizquierda o izquierda. "
                                     "Puede que el tema esté infrarrepresentado (o enfocado distinto) en la derecha."})
    total = len(articles)
    if total >= 8 and groups:
        top = groups.most_common(2)
        top_share = sum(n for _, n in top) / total
        if top_share >= 0.5 and top[0][0] != "Sin identificar":
            names = " y ".join(name for name, _ in top)
            warnings.append({"kind": "concentration", "title": "Cobertura concentrada",
                             "text": f"El {round(top_share * 100)} % de los resultados pertenece a {names}. "
                                     "Varios titulares pueden compartir línea editorial y propietarios."})

    by_media = [(media_ref[name], name, n) for name, n in media_counts.most_common()]
    return Coverage(
        total=total,
        outlets=len(media_counts),
        rated=rated,
        by_position=by_position,
        by_group=groups.most_common(),
        by_media=by_media,
        left_share=share(left),
        center_share=share(center),
        right_share=share(right),
        warnings=warnings,
        agency_headlines=sum(agencies.values()),
        agency_names=[n for n, _ in agencies.most_common()],
    )
