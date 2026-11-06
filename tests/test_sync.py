"""Synchronizace: jen fast-forward, jen čistá repa; nikdy merge commit, rebase, reset ani push."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from repo_doctor import cli
from repo_doctor.sync import fast_forward, status
from tests.factory import RepoBuilder, git

pytestmark = pytest.mark.usefixtures("allow_file")


def _setup(tmp_path: Path, name: str = "obet") -> tuple[RepoBuilder, RepoBuilder]:
    """(lokální klon pozadu, klon, ze kterého se pushují novinky)."""
    upstream = tmp_path / f"{name}-up.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    other = RepoBuilder.create(tmp_path / f"{name}-jinde")
    other.write("a.txt", "a\n").commit("init")
    other.remote("origin", str(upstream))
    other.git("push", "-q", "-u", "origin", "main")
    local = RepoBuilder.create(tmp_path / "projekty" / name)
    local.remote("origin", str(upstream))
    local.git("fetch", "-q", "origin")
    local.git("checkout", "-q", "-B", "main", "--track", "origin/main")
    other.write("b.txt", "b\n").commit("druhý")
    other.write("a.txt", "a2\n").commit("třetí")
    other.git("push", "-q", "origin", "main")
    return local, other


def test_fast_forward_behind_repo(tmp_path: Path) -> None:
    local, other = _setup(tmp_path)
    before = local.head()
    st = status(local.path, fetch=True)
    assert st.state == "behind" and st.behind == 2 and st.can_fast_forward
    assert sorted(st.files) == ["a.txt", "b.txt"]
    result = fast_forward([local.path])
    assert result.updated == [(local.path, 2)]
    assert local.head() == other.head()  # přesně upstream – žádný merge commit
    assert git(local.path, "rev-list", "--count", f"{before}..HEAD").strip() == "2"
    assert (local.path / "a.txt").read_text() == "a2\n"
    assert status(local.path).state == "clean"


def test_hooks_never_run_during_fast_forward(tmp_path: Path) -> None:
    local, _ = _setup(tmp_path)
    marker = tmp_path / "HOOK"
    hook = local.path / ".git" / "hooks" / "post-merge"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    status(local.path, fetch=True)
    assert fast_forward([local.path]).updated
    assert not marker.exists()


@pytest.mark.parametrize(
    "case", ["dirty", "untracked-clash", "ignored-clash", "diverged", "filters", "worktree"]
)
def test_refused_cases_leave_repo_untouched(tmp_path: Path, case: str) -> None:
    local, _ = _setup(tmp_path)
    status(local.path, fetch=True)
    if case == "dirty":
        local.write("a.txt", "rozdělané\n")
    elif case == "untracked-clash":
        local.write("b.txt", "můj nesledovaný\n")
    elif case == "ignored-clash":
        (local.path / ".git" / "info").mkdir(exist_ok=True)
        (local.path / ".git" / "info" / "exclude").write_text("b.txt\n")
        local.write("b.txt", "MYSECRET\n")  # ignorovaný lokální soubor (třeba .env)
    elif case == "diverged":
        local.write("c.txt", "c").commit("lokální")
    elif case == "filters":
        local.git("config", "filter.tajne.smudge", "cat")
    else:  # cizí .git/config přesměruje pracovní strom (např. do $HOME)
        victim = tmp_path / "obet-home"
        victim.mkdir()
        (victim / "a.txt").write_text("a\n")
        local.git("config", "core.worktree", str(victim))
    before = local.snapshot()
    st = status(local.path)
    assert not st.can_fast_forward and st.reason
    result = fast_forward([local.path])
    assert result.updated == [] and result.skipped
    assert local.snapshot() == before
    if case == "worktree":
        assert sorted(p.name for p in (tmp_path / "obet-home").iterdir()) == ["a.txt"]


def test_other_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local, _ = _setup(tmp_path)
    assert status(local.path).state == "clean"  # bez fetch o novinkách neví
    local.write("x", "x").commit("ahead")
    assert status(local.path).state == "ahead"
    local.git("checkout", "-q", "--detach")
    assert status(local.path).state == "detached"
    local.git("checkout", "-q", "-b", "bez-upstreamu")
    assert status(local.path).state == "no-upstream"
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    st = status(local.path)
    assert st.state == "untrusted" and not st.can_fast_forward


def test_reverified_before_apply(tmp_path: Path) -> None:
    local, _ = _setup(tmp_path)
    assert status(local.path, fetch=True).can_fast_forward
    local.write("a.txt", "změna mezi náhledem a potvrzením\n")
    result = fast_forward([local.path])
    assert result.updated == [] and "necommitnuté" in result.skipped[0][1]


def test_cli_sync(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    local, other = _setup(tmp_path)
    root = str(tmp_path / "projekty")
    assert cli.main(["sync", root]) == 0  # výchozí bez fetch – nic nového nevidí
    assert "aktuální" in capsys.readouterr().out
    assert cli.main(["sync", root, "--fetch"]) == 0
    out = capsys.readouterr().out
    assert "pozadu" in out and "dožene 2 commity" in out and "Nic se nezměnilo" in out
    assert cli.main(["sync", str(tmp_path / "neni")]) == 2
    assert local.head() != other.head()
    assert cli.main(["sync", root, "--apply"]) == 0
    assert "Dohnáno fast-forwardem: 1 repo" in capsys.readouterr().out
    assert local.head() == other.head()


@pytest.mark.skipif(sys.platform == "win32", reason="cesty")
@pytest.mark.parametrize("size", [(80, 24), (160, 48)], ids=["80x24", "160x48"])
def test_sync_screen_snapshot(snap_compare: object, tmp_path: Path, size: tuple[int, int]) -> None:
    from repo_doctor.models import RepoResult, ScanResult
    from tests.tui_helpers import make_app

    behind, _ = _setup(tmp_path, "pozadu")
    git(behind.path, "fetch", "-q", "origin")
    diverged, _ = _setup(tmp_path, "rozdelene")
    git(diverged.path, "fetch", "-q", "origin")
    diverged.write("c.txt", "c").commit("lokální")
    clean, _ = _setup(tmp_path, "aktualni")
    result = ScanResult(
        repos=[
            RepoResult(path=str(r.path), name=r.path.name, root="~")
            for r in (behind, diverged, clean)
        ]
    )

    async def run_before(pilot: object) -> None:
        await pilot.press("7")  # type: ignore[attr-defined]
        await pilot.pause(0.3)  # type: ignore[attr-defined]
        await pilot.app.workers.wait_for_complete()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]

    app = make_app(tmp_path, result=result)
    assert snap_compare(app, terminal_size=size, run_before=run_before)  # type: ignore[operator]


async def test_sync_screen_confirm_default_cancel_then_apply(tmp_path: Path) -> None:
    from repo_doctor.models import RepoResult, ScanResult
    from repo_doctor.tui.screens.dialogs import ConfirmDialog
    from repo_doctor.tui.screens.sync import SyncScreen
    from tests.tui_helpers import make_app

    local, other = _setup(tmp_path)
    result = ScanResult(repos=[RepoResult(path=str(local.path), name="obet", root="~")])
    app = make_app(tmp_path, result=result)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("7")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SyncScreen)
        assert screen.chosen() == []  # bez fetch je repo „aktuální“
        await pilot.press("f")
        await pilot.pause(0.5)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [s.name for s in screen.chosen()] == ["obet"]
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("enter")  # Zrušit
        await pilot.pause()
        assert local.head() != other.head()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause(0.5)
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert local.head() == other.head()
