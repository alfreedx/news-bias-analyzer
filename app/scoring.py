"""Turn externally published labels into positions on a common scale.

Nothing here is an opinion about a specific outlet: every outlet's position is
derived only from labels that third parties published (see ``data/media.json``),
using the fixed mapping tables below. The tables are shown verbatim on the
methodology page, so anyone can audit or challenge them.

Scale (left-right axis): -2 izquierda · -1 centroizquierda · 0 centro ·
+1 centroderecha · +2 derecha.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


def _norm(label: str) -> str:
    text = unicodedata.normalize("NFD", label.strip().lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


# Left-right mapping of the labels used by our sources. Keys are normalised
# (lower-case, no accents). Labels that describe the territorial axis are kept
# apart in TERRITORIAL_LABELS and do not move the left-right position.
IDEOLOGY_MAP: dict[str, int] = {
    # Media Bias/Fact Check
    "left": -2,
    "left-center": -1,
    "least biased": 0,
    "right-center": 1,
    "right": 2,
    # Wikipedia / Wikidata (es)
    "comunismo": -2,
    "anarquismo": -2,
    "anarcosindicalista": -2,
    "izquierdas": -2,
    "izquierda": -2,
    "izquierda abertzale": -2,
    "socialismo": -2,
    "socialismo democratico": -2,
    "progresismo": -1,
    "socialdemocracia": -1,
    "centroizquierda": -1,
    "republicanismo": -1,
    "laicismo": -1,
    "feminismo": -1,
    "politica verde": -1,
    "centro": 0,
    "centrismo": 0,
    "liberalismo": 0,
    "centroderecha": 1,
    "conservadurismo": 1,
    "conservadurismo liberal": 1,
    "conservadurismo social": 1,
    "liberalismo economico": 1,
    "neoliberalismo": 1,
    "monarquismo": 1,
    "catolicismo": 1,
    "cristianismo": 1,
    "conservador": 1,
    "derecha": 2,
    "anticomunismo": 2,
    "nacionalismo": 2,
    "tradicionalismo politico espanol": 2,
}

# Labels shown as quoted tags that place an outlet on neither axis.
DESCRIPTIVE_LABELS: frozenset[str] = frozenset(
    {"localismo", "movimientos sociales", "libertario", "antiautoritarismo"}
)

TERRITORIAL_LABELS: dict[str, str] = {
    "catalanismo": "Catalanismo",
    "nacionalismo catalan": "Nacionalismo catalán",
    "independentismo catalan": "Independentismo catalán",
    "nacionalismo vasco": "Nacionalismo vasco",
    "independentismo vasco": "Independentismo vasco",
    "foralismo": "Foralismo",
    "navarrismo": "Navarrismo",
    "regionalismo": "Regionalismo",
    "constitucionalismo": "Constitucionalismo",
}

FACTUAL_MAP: dict[str, tuple[int, str]] = {
    "very high": (4, "Muy alta"),
    "high": (3, "Alta"),
    "mostly factual": (2, "Mayormente fiable"),
    "mixed": (1, "Mixta"),
    "low": (0, "Baja"),
    "very low": (0, "Muy baja"),
}

POSITION_LABELS = {
    -2: "Izquierda",
    -1: "Centroizquierda",
    0: "Centro",
    1: "Centroderecha",
    2: "Derecha",
}

SOURCE_NAMES = {
    "mbfc": "Media Bias/Fact Check",
    "wikipedia": "Wikipedia (es)",
    "wikidata": "Wikidata",
}


def bucket(score: float) -> int:
    """Round a mean score to the closest point of the 5-point scale."""
    if score <= -1.5:
        return -2
    if score <= -0.5:
        return -1
    if score < 0.5:
        return 0
    if score < 1.5:
        return 1
    return 2


@dataclass
class Tag:
    """A label exactly as an external source published it (never our own wording)."""

    label: str
    source: str
    url: str
    axis: str  # "ideology" | "territorial" | "descriptive"
    revid: int | None = None
    retrieved: str | None = None

    @property
    def permalink(self) -> str:
        if self.revid and "wikipedia.org/wiki/" in self.url:
            base = self.url.split("/wiki/", 1)[0]
            return f"{base}/w/index.php?oldid={self.revid}"
        return self.url


@dataclass
class EvidenceScore:
    source: str
    labels: list[str]
    position: float | None
    url: str
    quote: str | None = None
    revid: int | None = None
    retrieved: str | None = None

    @property
    def permalink(self) -> str:
        return Tag("", self.source, self.url, "", self.revid).permalink


@dataclass
class Assessment:
    score: float | None = None
    position: int | None = None
    label: str = "Sin etiqueta publicada"
    agreement: str | None = None
    territorial: list[str] = field(default_factory=list)
    factual_level: int | None = None
    factual_label: str = "Sin evaluación externa"
    factual_url: str | None = None
    ifcn: bool = False
    ifcn_evidence: dict | None = None
    evidence: list[EvidenceScore] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    # CIS public perception (app/perception.py): shown apart from labels. ``by_perception`` is True only
    # when the position itself comes from it, because the outlet has no published ideology label.
    perception: tuple = ()
    by_perception: bool = False

    @property
    def rated(self) -> bool:
        return self.position is not None


def assess(evidence: list[dict]) -> Assessment:
    """Combine the evidence items of one outlet into an assessment."""
    result = Assessment()
    positions: list[float] = []
    territorial: list[str] = []

    for item in evidence:
        kind = item.get("kind")
        labels = item.get("labels", [])
        if kind == "ideology":
            values = [IDEOLOGY_MAP[_norm(lbl)] for lbl in labels if _norm(lbl) in IDEOLOGY_MAP]
            for lbl in labels:
                key = _norm(lbl)
                terr = TERRITORIAL_LABELS.get(key)
                if terr and terr not in territorial:
                    territorial.append(terr)
                axis = "territorial" if terr else ("ideology" if key in IDEOLOGY_MAP else "descriptive")
                result.tags.append(Tag(lbl, SOURCE_NAMES.get(item["source"], item["source"]), item["url"], axis,
                                       item.get("revid"), item.get("retrieved")))
            pos = sum(values) / len(values) if values else None
            if pos is not None:
                positions.append(pos)
            result.evidence.append(
                EvidenceScore(
                    source=SOURCE_NAMES.get(item["source"], item["source"]),
                    labels=labels,
                    position=pos,
                    url=item["url"],
                    quote=item.get("quote"),
                    revid=item.get("revid"),
                    retrieved=item.get("retrieved"),
                )
            )
        elif kind == "factual" and labels:
            mapped = FACTUAL_MAP.get(_norm(labels[0]))
            if mapped:
                result.factual_level, result.factual_label = mapped
                result.factual_url = item["url"]
        elif kind == "ifcn":
            result.ifcn = True
            result.ifcn_evidence = item

    result.territorial = territorial
    if positions:
        result.score = round(sum(positions) / len(positions), 2)
        result.position = bucket(result.score)
        result.label = POSITION_LABELS[result.position]
        spread = max(positions) - min(positions)
        if len(positions) == 1:
            result.agreement = "Una sola fuente"
        elif spread <= 0.5:
            result.agreement = "Fuentes coinciden"
        elif spread <= 1.0:
            result.agreement = "Fuentes coinciden parcialmente"
        else:
            result.agreement = "Fuentes discrepan"
    if result.ifcn and result.factual_level is None:
        result.factual_label = "Verificador acreditado (IFCN)"
    return result
