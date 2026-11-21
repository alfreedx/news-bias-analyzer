"""Public perception of outlets: the CIS survey (data/perception.json).

This is a *perception*, never a label: it is shown on its own and is never mixed into the
mean of published labels (``scoring.assess``). Its only effect on the position is a fallback
for outlets that no source labels, using the fixed equal-width bands below (shown verbatim on
the methodology page).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SCALE_MIN, SCALE_MAX = 1.0, 10.0

# Five equal-width bands of the 1-10 scale (width 1.8). Each upper edge belongs to its own band:
# [1, 2.8] · (2.8, 4.6] · (4.6, 6.4] · (6.4, 8.2] · (8.2, 10]. Values are position -2 … +2.
BAND_WIDTH = round((SCALE_MAX - SCALE_MIN) / 5, 1)  # 1.8
UPPER_EDGES: tuple[float, ...] = (2.8, 4.6, 6.4, 8.2)
BAND_INTERVALS: tuple[tuple[int, str], ...] = (
    (-2, "de 1 a 2,8 (ambos incluidos)"),
    (-1, "más de 2,8 y hasta 4,6"),
    (0, "más de 4,6 y hasta 6,4"),
    (1, "más de 6,4 y hasta 8,2"),
    (2, "más de 8,2 y hasta 10"),
)

# CIS question number asked for each kind of medium.
QUESTION_BY_KIND = {"periódico": 22, "cadena de televisión": 23, "emisora de radio": 24}


def band(mean: float) -> int:
    """Position (-2 … +2) of a CIS mean on the equal-width bands."""
    for position, edge in zip((-2, -1, 0, 1), UPPER_EDGES, strict=True):
        if mean <= edge:
            return position
    return 2


@dataclass(frozen=True)
class PerceptionEntry:
    """One CIS medium (name exactly as the CIS writes it) mapped to a catalog outlet."""

    cis_name: str
    kind: str
    question: int
    mean: float
    sd: float
    n: int


def position_of(entries: tuple[PerceptionEntry, ...] | list[PerceptionEntry]) -> int | None:
    """Position given by the CIS entries of one outlet; None when they fall in different bands."""
    bands = {band(e.mean) for e in entries}
    return bands.pop() if len(bands) == 1 else None


@dataclass(frozen=True)
class PerceptionData:
    meta: dict
    media: dict[str, tuple[PerceptionEntry, ...]]
    unmapped: tuple[dict, ...] = ()  # CIS media with no catalog outlet (kept for the record, never shown as data)


def _entry(outlet: str, raw: dict) -> PerceptionEntry:
    entry = PerceptionEntry(raw["cis_name"], raw["kind"], int(raw["question"]),
                            float(raw["mean"]), float(raw["sd"]), int(raw["n"]))
    if QUESTION_BY_KIND.get(entry.kind) != entry.question:
        raise ValueError(f"perception.json: {outlet}/{entry.cis_name}: kind {entry.kind!r} "
                         f"does not match question {entry.question}")
    if not SCALE_MIN <= entry.mean <= SCALE_MAX:
        raise ValueError(f"perception.json: {outlet}/{entry.cis_name}: mean {entry.mean} outside 1-10")
    if entry.sd < 0 or entry.n <= 0:
        raise ValueError(f"perception.json: {outlet}/{entry.cis_name}: invalid sd or n")
    return entry


def load_perception(directory: Path, known_ids: set[str] | None = None) -> PerceptionData:
    """Read data/perception.json (empty when absent). Unknown outlet ids fail fast."""
    path = directory / "perception.json"
    if not path.exists():
        return PerceptionData({}, {})
    raw = json.loads(path.read_text(encoding="utf-8"))
    media = {oid: tuple(_entry(oid, e) for e in entries) for oid, entries in raw["media"].items()}
    for oid, entries in media.items():
        if not entries:
            raise ValueError(f"perception.json: {oid} has no entries")
    if known_ids is not None and (unknown := sorted(set(media) - known_ids)):
        raise ValueError(f"perception.json names unknown outlets: {unknown}")
    return PerceptionData(raw["_meta"], media, tuple(raw.get("unmapped", ())))
