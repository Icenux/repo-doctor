"""Skóre repozitáře (0–100) a triážní pásma."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from repo_doctor.i18n import _
from repo_doctor.models import Finding, RepoResult, Severity

PENALTY = {Severity.HIGH: 25, Severity.MEDIUM: 9, Severity.LOW: 3}
# Každá kontrola může skóre srazit nejvýš o tolik (deset LOW nálezů jedné kontroly ≠ katastrofa).
PER_CHECK_CAP = {Severity.HIGH: 50, Severity.MEDIUM: 18, Severity.LOW: 6}
# Do 30 bodů platí 100 − srážka. Níž se skóre jen blíží nule (#37), aby se repa s mnoha
# vážnými nálezy dál lišila a žádné nemělo 0. Přechod je spojitý a se stejným sklonem.
LINEAR_FLOOR = 30


def penalty(findings: Iterable[Finding]) -> int:
    """Celková srážka bodů (součet za kontroly, každá omezená stropem)."""
    per_check: dict[tuple[str, Severity], int] = {}
    for f in findings:
        key = (f.check_id, f.severity)
        per_check[key] = min(PER_CHECK_CAP[f.severity], per_check.get(key, 0) + PENALTY[f.severity])
    return sum(per_check.values())


def score(findings: Iterable[Finding]) -> int:
    lost = penalty(findings)
    if lost <= 100 - LINEAR_FLOOR:
        return 100 - lost
    # hyperbola 900 / (srážka − 40): v bodě 70 dává 30 se sklonem −1, k nule se jen blíží
    tail = LINEAR_FLOOR**2 / (lost - (100 - 2 * LINEAR_FLOOR))
    return max(1, round(tail))


class Band(StrEnum):
    """Skupina v triáži: podle nejzávažnějšího nálezu repa (#73)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"
    UNCHECKED = "unchecked"

    @property
    def label(self) -> str:
        return {
            "high": "HIGH",
            "medium": "MED",
            "low": "LOW",
            "none": _("BEZ NÁLEZŮ"),
            "unchecked": _("NELZE ZKONTROLOVAT"),
        }[self.value]

    @property
    def order(self) -> int:
        return list(Band).index(self)


def is_no_pulse(repo: RepoResult, days: int = 90, now: datetime | None = None) -> bool:
    last = repo.state.last_commit
    if last is None:
        return repo.state.commit_count == 0 and repo.state.head is None
    now = now or datetime.now(UTC)
    return now - last > timedelta(days=days)


def band(repo: RepoResult) -> Band:
    """Skupina podle nejzávažnějšího nálezu; repo bez skóre (nešlo zkontrolovat) je zvlášť."""
    if repo.score is None:
        return Band.UNCHECKED
    return {
        Severity.HIGH: Band.HIGH,
        Severity.MEDIUM: Band.MEDIUM,
        Severity.LOW: Band.LOW,
        None: Band.NONE,
    }[repo.worst]


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int], width: int | None = None) -> str:
    """Sparkline z hodnot; při `width` se hodnoty sečtou do `width` košů."""
    if width is not None and values and len(values) != width:
        buckets = [0] * width
        for i, v in enumerate(values):
            buckets[min(width - 1, i * width // len(values))] += v
        values = buckets
    if not values:
        return ""
    top = max(values)
    if top <= 0:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(7, round(v / top * 7))] if v else SPARK[0] for v in values)


def sort_score(score_value: int | None) -> int:
    """Řazení vzestupně podle skóre; repo bez skóre až na konec."""
    return 101 if score_value is None else score_value


def score_label(score_value: int | None) -> str:
    """Skóre k zobrazení; repo, které nešlo zkontrolovat, má „–“."""
    return "–" if score_value is None else str(score_value)


def gauge(score_value: int | None, blocks: int = 5) -> str:
    if score_value is None:
        return "·" * blocks
    filled = max(0, min(blocks, round(score_value / 100 * blocks)))
    if score_value > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (blocks - filled)
