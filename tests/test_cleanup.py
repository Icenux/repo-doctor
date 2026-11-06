"""Úklid místa: jen ignorované regenerovatelné složky, přes koš, s ověřením před smazáním."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from repo_doctor import cli
from repo_doctor.checks import get_check
from repo_doctor.cleanup import (
    Candidate,
    apply_cleanup,
    find_candidates,
    human,
    inspect_repo,
    mark_busy,
    move_to_trash,
)
from repo_doctor.config import CleanupConfig, Config
from tests.factory import RepoBuilder
from tests.helpers import ctx

OLD_TS = 1_600_000_000  # 2020 – neaktivní repo


def _repo(tmp_path: Path, name: str = "stare", *, ts: int | None = OLD_TS) -> RepoBuilder:
    rb = RepoBuilder.create(tmp_path / "projekty" / name)
    rb.write(".gitignore", "node_modules/\n.venv/\nbuild/\n")
    rb.write("src/main.py", "print(1)\n")
    rb.commit("init", ts=ts)
    (rb.path / "node_modules" / "lib").mkdir(parents=True)
    (rb.path / "node_modules" / "lib" / "index.js").write_bytes(b"x" * 300_000)
    (rb.path / "web" / "node_modules").mkdir(parents=True)
    (rb.path / "web" / "node_modules" / "a.js").write_bytes(b"y" * 100_000)
    # sledovaná složka jménem build – nesmí se nabídnout
    rb.write("dist/keep.txt", "sledováno").commit("dist", ts=ts)
    return rb


def test_find_candidates_only_ignored_untracked_dirs(tmp_path: Path) -> None:
    rb = _repo(tmp_path)
    (rb.path / ".venv").symlink_to(tmp_path)  # symlink se nikdy nenabídne
    found = {c.rel: c.size for c in find_candidates(rb.path)}
    assert found == {"node_modules": 300_000, "web/node_modules": 100_000}


def test_trash_follows_freedesktop_spec(tmp_path: Path) -> None:
    rb = _repo(tmp_path)
    target = move_to_trash(rb.path / "node_modules")
    trash = Path(os.environ["XDG_DATA_HOME"]) / "Trash"
    assert target == trash / "files" / "node_modules"
    info = (trash / "info" / "node_modules.trashinfo").read_text()
    assert info.startswith("[Trash Info]\nPath=") and "DeletionDate=" in info
    # stejné jméno podruhé → přípona
    (rb.path / "node_modules").mkdir()
    assert move_to_trash(rb.path / "node_modules").name == "node_modules.2"


def test_trash_symlink_or_open_permissions_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repo_doctor.cleanup as cl

    rb = _repo(tmp_path)
    planted = tmp_path / ".Trash-x"
    elsewhere = tmp_path / "cizi"
    elsewhere.mkdir()
    planted.symlink_to(elsewhere)
    monkeypatch.setattr(cl, "trash_dir_for", lambda p: planted)
    with pytest.raises(OSError, match="obyčejná složka"):
        move_to_trash(rb.path / "node_modules")
    planted.unlink()
    planted.mkdir(mode=0o777)
    planted.chmod(0o777)
    with pytest.raises(OSError, match="ostatním"):
        move_to_trash(rb.path / "node_modules")
    assert (rb.path / "node_modules").is_dir() and not any(elsewhere.iterdir())


def test_apply_reverifies_and_skips_busy_and_outside(tmp_path: Path) -> None:
    rb = _repo(tmp_path)
    before = rb.snapshot()
    cands = find_candidates(rb.path)
    outside = Candidate(rb.path, tmp_path, 1)
    tracked = Candidate(rb.path.resolve(), (rb.path / "dist").resolve(), 1)
    result = apply_cleanup([*cands, outside, tracked])
    assert sorted(p.name for p, _ in result.removed) == ["node_modules", "node_modules"]
    reasons = dict(result.skipped)
    assert reasons[tmp_path] == "mimo repozitář"
    assert "sledované" in reasons[(rb.path / "dist").resolve()]
    assert not (rb.path / "node_modules").exists() and (rb.path / "dist/keep.txt").exists()
    after = rb.snapshot()
    assert after["status"] == before["status"] and after["head"] == before["head"]


def test_busy_detection(tmp_path: Path) -> None:
    rb = _repo(tmp_path)
    cands = find_candidates(rb.path)
    mark_busy(cands, busy=[str(rb.path.resolve() / "node_modules" / "lib" / "index.js")])
    assert {c.rel: c.busy for c in cands} == {"node_modules": True, "web/node_modules": False}
    result = apply_cleanup(cands, remover=lambda p: None)
    assert "běží" in dict(result.skipped).get(cands[0].path, "") or len(result.removed) == 2


def test_untrusted_owner_never_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rb = _repo(tmp_path)
    cands = find_candidates(rb.path)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    assert inspect_repo(rb.path).error
    result = apply_cleanup(cands)
    assert result.removed == [] and (rb.path / "node_modules").exists()


def test_disk_usage_check(tmp_path: Path) -> None:
    check = get_check("disk-usage")
    assert check is not None
    small = Config(cleanup=CleanupConfig(min_mb=1))
    old = _repo(tmp_path)
    assert check.run(ctx(old.path, small)) == []  # 400 kB < 1 MB
    (old.path / "node_modules" / "big.bin").write_bytes(b"z" * 1_200_000)
    findings = check.run(ctx(old.path, small))
    assert len(findings) == 1 and "1.5 MB" in findings[0].message
    fresh = _repo(tmp_path, "nove", ts=1_790_000_000)
    (fresh.path / "node_modules" / "big.bin").write_bytes(b"z" * 1_200_000)
    assert check.run(ctx(fresh.path, small)) == []  # aktivní repo


def test_human() -> None:
    assert human(512) == "512 B" and human(2048) == "2.0 kB" and human(3 * 1024**3) == "3.0 GB"


def test_cli_clean_dry_run_then_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rb = _repo(tmp_path)
    dirty = _repo(tmp_path, "rozdelane")
    dirty.write("src/main.py", "print(2)\n")
    root = str(tmp_path / "projekty")
    assert cli.main(["clean", root]) == 0
    out = capsys.readouterr().out
    assert "node_modules" in out and "Nic se nesmazalo" in out and "--include-dirty" in out
    assert (rb.path / "node_modules").exists()
    assert cli.main(["clean", root, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "Přesunuto do koše: 2" in out
    assert not (rb.path / "node_modules").exists()
    assert (dirty.path / "node_modules").exists()  # repo se změnami bez --include-dirty ne
    assert cli.main(["clean", root, "--apply", "--include-dirty"]) == 0
    assert not (dirty.path / "node_modules").exists()
    assert (dirty.path / "src/main.py").read_text() == "print(2)\n"
    capsys.readouterr()
    assert cli.main(["clean", root]) == 0
    assert "Nic k úklidu." in capsys.readouterr().out
    active = _repo(tmp_path, "aktivni", ts=int(time.time()) - 86400)
    assert cli.main(["clean", str(active.path.parent)]) == 0
    assert "s nedávnou aktivitou přeskočeno" in capsys.readouterr().out
    assert cli.main(["clean", str(tmp_path / "neni")]) == 2


def test_duplicate_clone_reverified_before_trash(tmp_path: Path) -> None:
    from tests.factory import git

    rb = _repo(tmp_path)
    clone = Candidate(rb.path.resolve(), rb.path.resolve(), 1, "clone")
    result = apply_cleanup([clone], remover=lambda p: None)
    assert "nemá remote" in result.skipped[0][1]  # bez remote nejde nepushnuté ověřit
    upstream = tmp_path / "upstream.git"
    git(tmp_path, "init", "-q", "--bare", str(upstream))
    rb.remote("origin", str(upstream))
    rb.git("push", "-q", "-u", "origin", "main")
    rb.write("src/main.py", "změna")
    result = apply_cleanup([clone], remover=lambda p: None)
    assert "necommitnuté" in result.skipped[0][1]
    rb.git("checkout", "--", "src/main.py")
    rb.write("src/main.py", "stash").git("stash", "-q")
    result = apply_cleanup([clone], remover=lambda p: None)
    assert "stashe" in result.skipped[0][1]
    rb.git("stash", "drop", "-q")
    wt = tmp_path / "wt"
    rb.git("worktree", "add", "-q", str(wt), "-b", "feature")
    result = apply_cleanup([clone], remover=lambda p: None)
    assert "pracovní stromy" in result.skipped[0][1]
    linked = Candidate(wt.resolve(), wt.resolve(), 1, "clone")
    assert "worktree" in apply_cleanup([linked], remover=lambda p: None).skipped[0][1]
    rb.git("worktree", "remove", str(wt))
    rb.write(".gitignore", "node_modules/\n.env\n").commit("ignore")
    rb.git("push", "-q")
    rb.write(".env", "TOKEN=x")
    assert ".env" in apply_cleanup([clone], remover=lambda p: None).skipped[0][1]
    (rb.path / ".env").unlink()
    removed: list[Path] = []
    result = apply_cleanup([clone], remover=removed.append)
    assert result.skipped == [] and removed == [rb.path.resolve()]


@pytest.mark.parametrize("size", [(80, 24), (160, 48)], ids=["80x24", "160x48"])
def test_cleanup_screen_snapshot(
    snap_compare: object, tmp_path: Path, size: tuple[int, int]
) -> None:
    from repo_doctor.models import RepoResult, ScanResult
    from tests.tui_helpers import make_app

    old = _repo(tmp_path)
    fresh = _repo(tmp_path, "aktivni", ts=1_790_000_000)
    result = ScanResult(
        repos=[
            RepoResult(path=str(old.path), name="stare", root="~"),
            RepoResult(path=str(fresh.path), name="aktivni", root="~"),
        ]
    )

    async def run_before(pilot: object) -> None:
        await pilot.press("6")  # type: ignore[attr-defined]
        await pilot.pause(0.3)  # type: ignore[attr-defined]
        await pilot.app.workers.wait_for_complete()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]

    app = make_app(tmp_path, result=result)
    assert snap_compare(app, terminal_size=size, run_before=run_before)  # type: ignore[operator]


async def test_cleanup_screen_confirm_default_cancel_and_trash(tmp_path: Path) -> None:
    from repo_doctor.models import RepoResult, ScanResult
    from repo_doctor.tui.screens.cleanup import CleanupScreen
    from repo_doctor.tui.screens.dialogs import ConfirmDialog
    from tests.tui_helpers import make_app

    rb = _repo(tmp_path)
    result = ScanResult(repos=[RepoResult(path=str(rb.path), name="stare", root="~")])
    app = make_app(tmp_path, result=result)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("6")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CleanupScreen)
        assert len(screen.chosen()) == 2  # neaktivní repo bez změn je předvybrané
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("enter")  # výchozí tlačítko = Zrušit
        await pilot.pause()
        assert (rb.path / "node_modules").exists()
        # trvalé smazání: dvě potvrzení, první zrušené → nic
        await pilot.press("D")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert (rb.path / "node_modules").exists()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert not (rb.path / "node_modules").exists()
    assert (Path(os.environ["XDG_DATA_HOME"]) / "Trash" / "files" / "node_modules").exists()


async def test_long_confirm_keeps_buttons_visible_at_80x24(tmp_path: Path) -> None:
    from rich.text import Text
    from textual.widgets import Button

    from repo_doctor.tui.screens.dialogs import ConfirmDialog
    from tests.tui_helpers import make_app

    app = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        body = Text("\n".join(f"  • /velmi/dlouha/cesta/{i}/node_modules" for i in range(40)))
        app.push_screen(ConfirmDialog("Trvale smazat", body, danger=True))
        await pilot.pause()
        for bid in ("#cancel", "#ok"):
            region = app.screen.query_one(bid, Button).region
            assert region.height and region.bottom <= 24, bid
        assert app.focused is app.screen.query_one("#cancel", Button)
