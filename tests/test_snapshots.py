"""Snapshot testy obrazovek TUI (odpovídají wireframům v docs/design/tui.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests import factory as fx
from tests.sample import sample_result
from tests.tui_helpers import make_app

SIZES = [(80, 24), (160, 48)]


def _fix_app(tmp: Path) -> Any:
    rb = fx.RepoBuilder.create(tmp / "infra-notes")
    rb.write(".gitignore", "node_modules/\ndist/\n").write("main.py", "print(1)\n").write(
        "util.py", "x=1\n"
    )
    rb.write("pyproject.toml", "[project]\nname='x'\n").commit()
    result = sample_result()
    result.repos[0].path = str(rb.path)
    return make_app(tmp, result=result)


async def _wait(pilot: Any) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_dashboard(snap_compare: Any, tmp_path: Path, size: tuple[int, int]) -> None:
    assert snap_compare(make_app(tmp_path), terminal_size=size)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_detail(snap_compare: Any, tmp_path: Path, size: tuple[int, int]) -> None:
    assert snap_compare(make_app(tmp_path), terminal_size=size, press=["enter", "j"])


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_fix_confirm(snap_compare: Any, tmp_path: Path, size: tuple[int, int]) -> None:
    async def run_before(pilot: Any) -> None:
        await pilot.press("f")
        await _wait(pilot)
        await pilot.press("enter")
        await pilot.pause()

    assert snap_compare(_fix_app(tmp_path), terminal_size=size, run_before=run_before)


# Velikost wireframů (100×31) – zdroj obrázků v dokumentaci.
def test_wireframe_dashboard(snap_compare: Any, tmp_path: Path) -> None:
    assert snap_compare(make_app(tmp_path), terminal_size=(100, 31))


def test_wireframe_detail(snap_compare: Any, tmp_path: Path) -> None:
    assert snap_compare(make_app(tmp_path), terminal_size=(100, 31), press=["enter"])


def test_wireframe_fix(snap_compare: Any, tmp_path: Path) -> None:
    async def run_before(pilot: Any) -> None:
        await pilot.press("f")
        await _wait(pilot)
        await pilot.press("enter")
        await pilot.pause()

    assert snap_compare(_fix_app(tmp_path), terminal_size=(100, 31), run_before=run_before)


def test_dashboard_unchecked_repo(snap_compare: Any, tmp_path: Path) -> None:
    """Repo cizího vlastníka: skóre „–“ a skupina NELZE ZKONTROLOVAT (80×24)."""
    from repo_doctor.checks.meta import UnsafeOwnership
    from repo_doctor.models import RepoResult

    result = sample_result()
    result.repos = result.repos[:2]
    result.repos.append(
        RepoResult(
            path="/srv/sdilene/cizi",
            name="cizi-repo",
            root="~/projekty",
            score=None,
            untrusted_owner=True,
            findings=[UnsafeOwnership().for_path("/srv/sdilene/cizi")],
        )
    )
    assert snap_compare(make_app(tmp_path, result=result), terminal_size=(80, 24))


def test_detail_unchecked_repo(snap_compare: Any, tmp_path: Path) -> None:
    """Karta repa cizího vlastníka (80×24): jen skóre „–“, vysvětlení a nález s postupem."""
    from repo_doctor.checks.meta import UnsafeOwnership
    from repo_doctor.models import RepoResult

    result = sample_result()
    result.repos = [
        RepoResult(
            path="/srv/sdilene/cizi",
            name="cizi-repo",
            root="~/projekty",
            score=None,
            untrusted_owner=True,
            findings=[UnsafeOwnership().for_path("/srv/sdilene/cizi")],
        )
    ]
    app = make_app(tmp_path, result=result)
    assert snap_compare(app, terminal_size=(80, 24), press=["enter"])
