"""Coverage analysis: who reports a topic, from which side, owned by whom."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .catalog import Catalog, Media
from .scoring import POSITION_LABELS
from .text import _WORD, fold, headline_signals, keywords

POSITIONS = [-2, -1, 0, 1, 2]


@dataclass
class Article:
    title: str
    url: str
    summary: str
    published: int
    media: Media | None
    source_name: str
    relevance: float = 0.0
    signals: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)

    @property
    def position(self) -> int | None:
        return self.media.assessment.position if self.media else None

    def to_public(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "published": self.published,
            "source": self.source_name,
            "media": self.media.to_public() if self.media else None,
            "signals": self.signals,
        }


@dataclass
class Story:
    """The same news event as told by one or more outlets."""

    articles: list[Article]
    score: float = 0.0
    seed: Article | None = None  # the most relevant headline, kept as the story's title

    @property
    def lead(self) -> Article:
        return self.seed or self.articles[0]

    @property
    def outlets(self) -> int:
        return len({a.source_name for a in self.articles})

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
            return "Medios sin etiqueta publicada"
        if pos[0] < 0 < pos[-1]:
            return "Contada desde ambos lados"
        if pos[-1] < 0:
            return "Solo medios de izquierda"
        if pos[0] > 0:
            return "Solo medios de derecha"
        return "Cobertura parcial"


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

    def to_public(self) -> dict:
        return {
            "total": self.total,
            "outlets": self.outlets,
            "rated": self.rated,
            "by_position": {POSITION_LABELS[k]: v for k, v in self.by_position.items()},
            "by_group": self.by_group,
            "shares": {"left": self.left_share, "center": self.center_share, "right": self.right_share},
            "warnings": self.warnings,
        }


def make_article(catalog: Catalog, *, title: str, url: str, summary: str, published: int,
                 media_id: str | None = None, source_url: str | None = None,
                 source_name: str | None = None) -> Article:
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
        signals=headline_signals(title),
        keys=keywords(title),
    )


def rank(articles: list[Article], bm25: dict[str, float] | None = None, now: int | None = None) -> None:
    """Relevance = text match blended with freshness (half-life: 3 days)."""
    now = now or int(time.time())
    for a in articles:
        text_score = -(bm25 or {}).get(a.url, -1.0)  # bm25() is negative: lower is better
        age_days = max(0.0, (now - a.published) / 86400)
        a.relevance = text_score * (0.5 + 0.5 * math.exp(-age_days / 4.3))
    articles.sort(key=lambda a: a.relevance, reverse=True)


def cluster(articles: list[Article], min_shared: int = 3, min_overlap: float = 0.5) -> list[Story]:
    """Group headlines about the same event (greedy, inverted index, overlap coefficient)."""
    stories: list[Story] = []
    seeds: list[set[str]] = []
    index: dict[str, list[int]] = defaultdict(list)
    for art in articles:
        best, best_score = None, 0.0
        if len(art.keys) >= 2:
            candidates = Counter(i for k in art.keys for i in index.get(k, ()))
            for idx, shared in candidates.items():
                if shared < min(min_shared, len(art.keys)):
                    continue
                overlap = shared / min(len(art.keys), len(seeds[idx]))
                if overlap >= min_overlap and overlap > best_score:
                    best, best_score = idx, overlap
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
        s.score = max(a.relevance for a in s.articles) * (1 + 0.4 * (s.outlets - 1))
        s.articles.sort(key=lambda a: (a.position is None, a.position if a.position is not None else 0))
    stories.sort(key=lambda s: s.score, reverse=True)
    return stories


def coverage(articles: list[Article]) -> Coverage:
    by_position = {p: 0 for p in POSITIONS}
    groups: Counter[str] = Counter()
    media_counts: Counter[str] = Counter()
    media_ref: dict[str, Media | None] = {}
    for a in articles:
        if a.position is not None:
            by_position[a.position] += 1
        if a.media and a.media.group:
            groups[a.media.group.name] += 1
        elif a.media:
            groups[a.media.publisher or f"{a.media.name} (independiente)"] += 1
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
    if total and rated / total < 0.5:
        warnings.append({"kind": "unrated", "title": "Muchos medios sin etiqueta publicada",
                         "text": "Más de la mitad de los resultados vienen de medios sin una evaluación externa "
                                 "publicada. El reparto ideológico es solo orientativo."})

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
    )
