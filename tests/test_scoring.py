from __future__ import annotations

import pytest

from repo_doctor.forges.urls import host_label
from repo_doctor.models import Category, Finding, Severity
from repo_doctor.scoring import Band, band, penalty, score
from repo_doctor.tui.triage_model import sort_key
from tests.sample import repo


def _f(check_id: str, sev: Severity, n: int = 1) -> list[Finding]:
    return [
        Finding(
            check_id=check_id,
            severity=sev,
            category=Category.MAINTENANCE,
            title=check_id,
            message=f"{check_id} {i}",
        )
        for i in range(n)
    ]


H, M, L = Severity.HIGH, Severity.MEDIUM, Severity.LOW


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        ([], 100),
        (_f("a", L), 97),
        (_f("a", L, 5), 94),  # strop kontroly 6
        (_f("a", M) + _f("b", M), 82),
        (_f("a", H), 75),
        (_f("a", H, 3), 50),  # strop kontroly 50
        (_f("a", H) + _f("b", M) + _f("c", M, 2), 48),
        (_f("a", H) + _f("b", M, 2) + _f("c", M), 48),
        (_f("a", H, 2) + _f("b", M, 2) + _f("c", L), 29),  # srážka 71 → už pod lineární částí
    ],
)
def test_score_is_linear_above_thirty(findings: list[Finding], expected: int) -> None:
    assert score(findings) == expected


def test_score_never_reaches_zero_and_keeps_order() -> None:
    """Dřív stačily HIGH nálezy dvou kontrol a skóre bylo 0 (#37)."""
    two_high = _f("a", H, 2) + _f("b", H, 2)
    assert penalty(two_high) == 100
    assert score(two_high) == 15
    worse = [*two_high, *_f("c", H, 2)]
    worst = [*worse, *_f("d", H, 2), *_f("e", M, 2), *_f("f", M, 2)]
    assert score(two_high) > score(worse) > score(worst) >= 1
    huge = [x for i in range(40) for x in _f(f"k{i}", H, 2)]
    assert score(huge) == 1


def test_scores_are_continuous_at_floor() -> None:
    # srážka 70 → 30, 71 → 29: žádný skok na hranici
    seventy = _f("a", H, 2) + _f("b", M, 2) + _f("c", L)
    assert penalty(seventy) == 71
    assert score(seventy) == 29
    assert score(_f("a", H, 2) + _f("b", M, 2)) == 32


def test_same_score_sorted_by_severity() -> None:
    a = repo("a", "/r", _f("x", H, 2) + _f("y", H, 2))
    b = repo("b", "/r", _f("x", H, 2) + _f("y", H, 2) + _f("z", M))
    a.score, b.score = 15, 15
    assert sorted([a, b], key=lambda r: sort_key(r, "score"))[0].name == "b"


@pytest.mark.parametrize(
    ("host", "label"),
    [
        ("github.com", "GitHub"),
        ("gitlab.com", "GitLab"),
        ("codeberg.org", "Codeberg"),
        ("git.example.org", "git"),
        ("100.121.109.115", "100.121.109.115"),
        ("[fd7a:115c::1]", "[fd7a:115c::1]"),
        ("localhost", "localhost"),
    ],
)
def test_host_label(host: str, label: str) -> None:
    assert host_label(host) == label


def test_band_follows_worst_finding_not_score_or_activity() -> None:
    """Skupina v triáži je dána nejzávažnějším nálezem; stáří repa ani skóre ji nemění (#73)."""
    many_low = _f("a", Severity.LOW) + _f("b", Severity.LOW) + _f("c", Severity.LOW)
    cases = [
        (_f("x", Severity.HIGH) + many_low, Band.HIGH),
        (_f("x", Severity.MEDIUM) + many_low, Band.MEDIUM),
        (many_low, Band.LOW),
        ([], Band.NONE),
    ]
    for findings, expected in cases:
        assert band(repo("r", "p", findings)) is expected
        assert band(repo("r", "p", findings, last_days=400)) is expected  # starý commit nevadí
