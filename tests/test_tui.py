from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx

from repo_doctor.config import ConfigStore
from repo_doctor.tui.app import RepoDoctorApp
from repo_doctor.tui.screens.dashboard import DashboardScreen, TriageList
from repo_doctor.tui.screens.detail import DetailScreen
from repo_doctor.tui.screens.dialogs import ConfirmDialog, InputDialog
from repo_doctor.tui.screens.export import ExportScreen
from repo_doctor.tui.screens.fix import FixScreen
from repo_doctor.tui.screens.folders import FoldersScreen
from repo_doctor.tui.screens.forges import ForgesScreen
from repo_doctor.tui.screens.forms import RootDialog
from repo_doctor.tui.screens.help import HelpScreen
from repo_doctor.tui.screens.remote import RemoteScreen
from repo_doctor.tui.screens.settings import SettingsScreen
from repo_doctor.tui.screens.wizard import WizardScreen
from repo_doctor.tui.theme import PALETTE, THEME_NAME
from tests import factory as fx
from tests.sample import sample_result
from tests.tui_helpers import CONFIG, fake_factory, make_app, screen_text


def dash(app: RepoDoctorApp) -> DashboardScreen:
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


async def test_dashboard_navigation(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        d = dash(app)
        first = app.selected_path
        assert first is not None and first.endswith("infra-notes")
        await pilot.press("j")
        assert app.selected_path != first
        await pilot.press("k")
        assert app.selected_path == first
        await pilot.press("G")
        await pilot.press("g")
        await pilot.press("ctrl+d", "ctrl+u")
        await pilot.press("g", "j")
        await pilot.press("enter")
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, DashboardScreen)
        # všechna repa jsou vidět; z sbalí skupinu vybraného repa a podruhé ji rozbalí
        all_repos = len(d.visible_repos())
        await pilot.press("G")
        await pilot.press("z")
        assert len(d.visible_repos()) < all_repos
        await pilot.press("z")
        assert len(d.visible_repos()) == all_repos
        text = screen_text(app)
        assert "HIGH" in text and "MED" in text and "BEZ TEPU" not in text


async def test_dashboard_sort_filter_group(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        d = dash(app)
        await pilot.press("S")
        assert d.state.sort == "name"
        await pilot.press("s")
        assert d.state.reverse
        await pilot.press("alt+1")
        assert all(any(f.severity.value == "high" for f in r.findings) for r in d.visible_repos())
        await pilot.press("alt+1")
        assert d.state.filters.min_severity is None
        await pilot.press("alt+2", "alt+3")
        assert (
            d.state.filters.min_severity is not None and d.state.filters.min_severity.value == "low"
        )
        await pilot.press("alt+3")
        await pilot.press("p")
        assert d.state.filters.problems_only
        await pilot.press("p")
        await pilot.press("alt+g")
        assert d.state.group_by == "root"
        await pilot.press("alt+g")
        assert d.state.group_by == "forge"
        await pilot.press("alt+g")
        assert d.state.group_by == "remote"
        await pilot.press("alt+g")
        assert d.state.group_by == "triage"
        await pilot.press("b")  # přepínač levého panelu zrušen (#89)
        assert d.query_one("#sidebar").display


async def test_sidebar_filters(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        d = dash(app)
        await pilot.press("tab")  # fokus na levý panel
        sidebar = d.query_one("#sidebar")
        assert app.focused is sidebar
        for oid in (
            "root:~/git-archiv",
            "forge:forgejo",
            "f:severity",
            "f:category",
            "f:problems",
            "f:group",
        ):
            ids = [o.id for o in sidebar.options]  # type: ignore[attr-defined]
            sidebar.highlighted = ids.index(oid)  # type: ignore[attr-defined]
            await pilot.press("enter")
        f = d.state.filters
        assert (
            f.root == "~/git-archiv"
            and f.forge == "forgejo"
            and f.problems_only
            and f.min_severity
            and f.category
        )
        assert d.state.group_by == "root"
        assert all(r.root == "~/git-archiv" and r.forge == "forgejo" for r in d.visible_repos())


async def test_search(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        d = dash(app)
        d.state.collapsed.clear()
        await pilot.press("slash")
        for ch in "tailscale":
            await pilot.press(ch)
        await pilot.pause()
        assert [r.name for r in d.visible_repos()] == ["infra-notes"]
        await pilot.press("enter")
        assert isinstance(app.focused, TriageList)
        await pilot.press("escape")
        assert d.state.filters.query == ""
        assert len(d.visible_repos()) > 1
        # hledání z jiné obrazovky se vrátí na přehled
        await pilot.press("1")
        assert isinstance(app.screen, FoldersScreen)
        await pilot.press("slash")
        assert isinstance(app.screen, DashboardScreen)


async def test_screen_switching_and_help(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        for key, cls in [
            ("1", FoldersScreen),
            ("2", ForgesScreen),
            ("4", SettingsScreen),
            ("5", ExportScreen),
            ("enter", DetailScreen),
        ]:
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, cls), key
            if cls is not DetailScreen:
                await pilot.press("escape")
        await pilot.press("escape")
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert "PŘEHLED (TRIÁŽ)" in screen_text(app)
        await pilot.press("j", "k", "escape")
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("t")  # přepínač tématu zrušen (#89): klávesa nedělá nic
        assert app.theme == THEME_NAME
        await pilot.press("O")
        assert app.offline
        await pilot.press("q")  # na přehledu jen poradí, aplikaci nezavře
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("ctrl+q")
    assert app.return_code == 0


async def test_detail_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    copied: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    def fake_copy(text: str) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr("repo_doctor.tui.screens.detail.open_url", fake_open)
    monkeypatch.setattr("repo_doctor.tui.screens.detail.wl_copy", fake_copy)
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        detail = app.screen
        assert isinstance(detail, DetailScreen)
        text = screen_text(app)
        assert "DIAGNÓZA" in text and "PROČ TO VADÍ" in text
        assert "AKIA…(20 znaků)" in text or "public-sensitive" in text
        await pilot.press("j", "j")
        await pilot.press("w", "y")
        assert opened == ["https://github.example/nekdo/infra-notes"]
        assert copied and copied[0].endswith("infra-notes")
        before = len(detail.repo.findings)  # type: ignore[union-attr]
        await pilot.press("a")
        assert isinstance(app.screen, InputDialog)
        await pilot.press("enter")  # prázdný důvod se nepřijme
        assert isinstance(app.screen, InputDialog)
        for ch in "fixtura":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert len(detail.repo.findings) == before - 1  # type: ignore[union-attr]
        assert app.config.allowlist[0].reason == "fixtura"
        assert "fixtura" in app.store.path.read_text()
        await pilot.press("o")  # soubor z historie neexistuje – jen upozornění, žádný pád
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FixScreen)


async def test_detail_keeps_selected_finding_on_rerender(tmp_path: Path) -> None:
    """Po zavření editoru (Resize) zůstane vybraný nález, ne první (#127)."""
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        detail = app.screen
        assert isinstance(detail, DetailScreen)
        await pilot.press("j", "j")
        chosen = detail.current
        assert chosen is not None
        name = detail.repo.name  # type: ignore[union-attr]
        detail.render_all()
        await pilot.pause()
        assert detail.current is not None
        assert detail.current.fingerprint(name) == chosen.fingerprint(name)


async def test_detail_without_fixable(tmp_path: Path) -> None:
    result = sample_result()
    app = make_app(tmp_path, result=result)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        app.open_detail(next(r.path for r in result.repos if r.name == "notes"))
        await pilot.pause()
        await pilot.press("f")
        assert isinstance(app.screen, DetailScreen)


async def test_progressive_scan_and_cancel(tmp_path: Path) -> None:
    repos = sample_result().repos
    app = make_app(
        tmp_path,
        result=sample_result().model_copy(update={"repos": []}),
        scanner_factory=fake_factory(repos, 0.2),
    )
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause(0.5)
        partial = len(app.result.repos)
        assert 0 < partial < len(repos)
        assert "sken" in screen_text(app)
        await pilot.press("R")  # druhý sken během běžícího se odmítne
        await pilot.press("x")
        await pilot.pause(0.3)
        assert not app.scanning
        assert len(app.result.repos) < len(repos)
        await pilot.press("x")  # nic neběží


async def test_cancel_hint_only_while_scanning(tmp_path: Path) -> None:
    repos = sample_result().repos[:4]
    app = make_app(
        tmp_path,
        result=sample_result().model_copy(update={"repos": []}),
        scanner_factory=fake_factory(repos, 0.2),
    )
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        assert "zruší sken" not in screen_text(app)
        await pilot.press("R")
        await pilot.pause(0.3)
        assert "zruší sken" in screen_text(app)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.scanning
        assert "zruší sken" not in screen_text(app)


async def test_full_scan_updates_and_saves(tmp_path: Path) -> None:
    repos = sample_result().repos[:4]
    app = make_app(tmp_path, result=sample_result(), scanner_factory=fake_factory(repos, 0.01))
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        app.start_scan()
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.result.repos) == 4
        from repo_doctor import paths

        assert paths.last_scan_file().exists()
        # sken jednoho repa
        await pilot.press("r")
        await app.workers.wait_for_complete()


async def test_auto_scan_with_real_scanner(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    fx.healthy_python_repo(root / "ok")
    leak = fx.RepoBuilder.create(root / "leak")
    leak.write("app.py", f'k = "{fx.fake_aws_key()}"\n').commit()
    cfg = f'[[roots]]\npath = "{root}"\n'
    app = make_app(
        tmp_path,
        config=cfg,
        result=sample_result().model_copy(update={"repos": []}),
        auto_scan=True,
        offline=True,
    )
    app.config.ui.scan_on_start = True
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause(0.5)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert {r.name for r in app.result.repos} == {"ok", "leak"}
        text = screen_text(app)
        assert fx.fake_aws_key() not in text


async def test_no_roots_warning(tmp_path: Path) -> None:
    app = make_app(tmp_path, config="")
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("R")
        assert not app.scanning


def _messy(tmp: Path) -> Path:
    rb = fx.RepoBuilder.create(tmp / "infra-notes")
    rb.write(".gitignore", "node_modules/\ndist/\n").write("main.py", "print(1)\n").write(
        "util.py", "x=1\n"
    )
    rb.write("pyproject.toml", "[project]\nname='x'\n").commit()
    return rb.path


def _result_for(path: Path) -> Any:
    result = sample_result()
    result.repos[0].path = str(path)
    return result


async def test_fix_cancel_then_apply(tmp_path: Path) -> None:
    repo = _messy(tmp_path)
    before = fx.RepoBuilder(repo).snapshot()
    app = make_app(tmp_path, result=_result_for(repo))
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        fix = app.screen
        assert isinstance(fix, FixScreen)
        assert [p.check_id for p in fix.patches] == [
            "gitignore-incomplete",
            "license-missing",
            "precommit-missing",
        ]
        text = screen_text(app)
        assert "NÁHLED ZMĚN" in text and "repo-doctor/fixes-2026-09-30" in text
        await pilot.press("j", "k")
        await pilot.press("space")  # odškrtne gitignore
        await pilot.press("A")  # vše
        await pilot.press("A")  # nic
        await pilot.press("enter")  # nic vybráno → jen upozornění
        assert isinstance(app.screen, FixScreen)
        await pilot.press("A")
        await pilot.press("enter")
        assert isinstance(app.screen, ConfirmDialog)
        assert app.focused is not None and app.focused.id == "cancel"  # výchozí volba = Zrušit
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, FixScreen)
        assert fx.RepoBuilder(repo).snapshot() == before  # po zrušení se nic nezměnilo
        await pilot.press("enter")
        await pilot.press("tab")
        assert app.focused is not None and app.focused.id == "ok"
        await pilot.press("enter")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
    after = fx.RepoBuilder(repo).snapshot()
    assert "refs/heads/repo-doctor/fixes-2026-09-30" in after["refs"]
    for key in ("head", "status", "index", "files"):
        assert after[key] == before[key]
    log = fx.git(repo, "log", "--format=%s", "repo-doctor/fixes-2026-09-30")
    assert log.count("chore(repo-doctor)") == 3


async def test_fix_refuses_dirty(tmp_path: Path) -> None:
    repo = _messy(tmp_path)
    (repo / "main.py").write_text("changed\n")
    app = make_app(tmp_path, result=_result_for(repo))
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "necommitnuté" in screen_text(app)
        await pilot.press("enter")
        assert isinstance(app.screen, FixScreen)
    assert "repo-doctor/fixes" not in fx.git(repo, "branch")


async def test_export(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    fx.register = None  # type: ignore[attr-defined]
    target = tmp_path / "out" / "report.json"
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("5")
        screen = app.screen
        assert isinstance(screen, ExportScreen)
        await pilot.press("down", "space")  # JSON
        await pilot.pause()
        assert screen.fmt == "json"
        path_input = screen.query_one("#path")
        path_input.value = str(target)  # type: ignore[attr-defined]
        await pilot.press("e")
        await pilot.pause()
    data = json.loads(target.read_text())
    assert data["schema_version"] == 2
    assert "AKIA…(20 znaků)" in target.read_text()


async def test_root_dialog_ignores_stale_depth_probe(tmp_path: Path) -> None:
    """Pozdní výsledek pro rozepsanou cestu nesmí přepsat návrh hloubky pro celou cestu."""
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.press("n")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, RootDialog)
        dialog.query_one("#path").value = str(tmp_path / "cela")  # type: ignore[attr-defined]
        depth = dialog.query_one("#depth")
        dialog._probed(str(tmp_path / "cela"), "hotovo", 1, 2)
        assert depth.value == "1"  # type: ignore[attr-defined]
        dialog._probed(str(tmp_path), "pozdní", 5, 9)  # výsledek pro kratší cestu
        assert depth.value == "1"  # type: ignore[attr-defined]


async def test_folders_crud(tmp_path: Path) -> None:
    newdir = tmp_path / "nove-repa"
    fx.RepoBuilder.create(newdir / "a")
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.press("n")
        for ch in str(newdir):
            await pilot.press(ch)
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert str(newdir) in app.store.path.read_text()
        assert app.config.roots[-1].depth == 1  # návrh hloubky se propsal do pole (#28)
        assert "# testovací konfigurace" in app.store.path.read_text()  # komentáře zůstaly
        table = app.screen.query_one("#roots")
        table.move_cursor(row=3)  # type: ignore[attr-defined]
        await pilot.press("space")
        assert not app.config.roots[3].enabled
        await pilot.press("e")
        await pilot.pause()
        depth = app.screen.query_one("#depth")
        depth.value = "5"  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.roots[3].depth == 5
        await pilot.press("d")
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("enter")  # Zrušit
        assert len(app.config.roots) == 4
        await pilot.press("d", "tab", "enter")
        await pilot.pause()
        assert len(app.config.roots) == 3
        # neexistující cesta se neuloží
        await pilot.press("n")
        for ch in "/nonexistent/xyz":
            await pilot.press(ch)
        await pilot.press("enter")
        assert "neexistuje" in screen_text(app)
        await pilot.press("escape")
        assert len(app.config.roots) == 3


@respx.mock
async def test_forges_crud_and_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "ghp_" + "Q" * 36
    monkeypatch.setenv("GH_TUI_TOKEN", token)
    respx.get("https://api.github.com/user").respond(json={"login": "nekdo"})
    respx.get("https://api.github.com/user/repos").respond(json=[{"full_name": "nekdo/a"}])
    app = make_app(tmp_path, config="")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.press("n")
        await pilot.pause()
        dialog = app.screen
        dialog.query_one("#name").value = "gh"  # type: ignore[attr-defined]
        dialog.query_one("#source").value = "env"  # type: ignore[attr-defined]
        await pilot.pause()
        dialog.query_one("#token_env").value = "GH_TUI_TOKEN"  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.forges[0].token_env == "GH_TUI_TOKEN"
        assert token not in app.store.path.read_text()
        await pilot.press("T")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = screen_text(app)
        assert "✓ nekdo" in text
        assert token not in text
        # duplicitní název se odmítne
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#name").value = "gh"  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        assert "už existuje" in screen_text(app)
        await pilot.press("escape")
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#verify").value = False  # type: ignore[attr-defined]
        await pilot.pause()
        assert "TLS je vypnuté" in screen_text(app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "vypnuto" in screen_text(app)
        await pilot.press("d", "tab", "enter")
        await pilot.pause()
        assert app.config.forges == []


async def test_forge_dialog_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        "repo_doctor.tui.screens.forges.store_in_keyring", lambda n, v: stored.__setitem__(n, v)
    )
    app = make_app(tmp_path, config="")
    secret = "tok-" + "k" * 30
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2", "n")
        await pilot.pause()
        app.screen.query_one("#type").value = "forgejo"  # type: ignore[attr-defined]
        app.screen.query_one("#url").value = "https://git.home.ts.net:3000"  # type: ignore[attr-defined]
        app.screen.query_one("#token").value = secret  # type: ignore[attr-defined]
        await pilot.pause()
        assert secret not in screen_text(app)  # heslové pole
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert stored == {"forgejo": secret}
        assert secret not in app.store.path.read_text()
        assert app.config.forges[0].token_source == "keyring"


async def test_wizard_first_run(tmp_path: Path) -> None:
    root = tmp_path / "moje-repa"
    fx.RepoBuilder.create(root / "g" / "r1")
    repos = sample_result().repos[:2]
    app = make_app(tmp_path, config=None, scanner_factory=fake_factory(repos, 0.01))
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WizardScreen)
        await pilot.press("ctrl+s")
        assert "aspoň jednu" in screen_text(app)
        for ch in str(root):
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.5)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.query_one("#depth").value == "2"  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DashboardScreen)
        await app.workers.wait_for_complete()
    cfg = ConfigStore(tmp_path / "config.toml").load()
    assert cfg.roots[0].path == str(root) and cfg.roots[0].depth == 2


async def test_wizard_skip(tmp_path: Path) -> None:
    app = make_app(tmp_path, config=None)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WizardScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, DashboardScreen)
    assert (tmp_path / "config.toml").exists()


async def test_keymap_remap(tmp_path: Path) -> None:
    repos = sample_result().repos[:1]
    app = make_app(
        tmp_path,
        config='[keys]\nscan_all = "F5"\nhelp = "F1"\n',
        scanner_factory=fake_factory(repos, 0.01),
        roots_override=[str(tmp_path)],
    )
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("f1")
        assert isinstance(app.screen, HelpScreen)
        assert "f5" in screen_text(app)
        await pilot.press("escape")
        await pilot.press("f5")
        await pilot.pause()
        assert app.scan_worker is not None
        await app.workers.wait_for_complete()


async def test_keymap_invalid_falls_back(tmp_path: Path) -> None:
    app = make_app(tmp_path, config='[keys]\nscan_all = "j"\n')
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        assert app.keymap_error and "kolize" in app.keymap_error


async def test_invalid_config_shows_error(tmp_path: Path) -> None:
    app = make_app(tmp_path, config='[[forges]]\nname="x"\ntype="github"\ntoken="plain"\n')
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        assert app.config_error and "prostý text" in app.config_error
        assert isinstance(app.screen, DashboardScreen)


async def test_command_palette_open_repo(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        for ch in "Otevřít repo: blog":
            await pilot.press(ch if ch != " " else "space")
        await pilot.pause(0.5)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.path.endswith("blog")


async def test_settings(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        config='[[allowlist]]\nhash = "0123456789abcdef"\nreason = "x"\ncheck = "stashes"\n',
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#limit-stash_days").value = "7"  # type: ignore[attr-defined]
        await pilot.pause()
        assert app.config.limits.stash_days == 7
        screen.query_one("#limit-stash_days").value = "abc"  # type: ignore[attr-defined]
        await pilot.pause()
        assert app.config.limits.stash_days == 7
        assert "celé číslo" in str(screen.query_one("#settings-error").render())
        screen.query_one("#limit-large_file_mb").value = "2.5"  # type: ignore[attr-defined]
        screen.query_one("#editor").value = "nvim"  # type: ignore[attr-defined]
        screen.query_one("#ignore_paths").value = "vendor/**, docs/**"  # type: ignore[attr-defined]
        screen.query_one("#templates_dir").value = "~/tpl"  # type: ignore[attr-defined]
        screen.query_one("#scan_on_start").value = "on"  # type: ignore[attr-defined]
        screen.query_one("#fail_on").value = "medium"  # type: ignore[attr-defined]
        screen.query_one("#license").value = "ISC"  # type: ignore[attr-defined]
        await pilot.pause()
        checks = screen.query_one("#checks")
        checks.deselect("stashes")  # type: ignore[attr-defined]
        await pilot.pause()
        await pilot.press("d")  # fokus není na allowlistu → nic se nesmaže
        await pilot.pause()
        assert len(app.config.allowlist) == 1
        table = screen.query_one("#allowlist")
        table.focus()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("enter")  # Zrušit
        assert len(app.config.allowlist) == 1
        await pilot.press("d", "tab", "enter")
        await pilot.pause()
    cfg = ConfigStore(tmp_path / "config.toml").load()
    assert cfg.limits.large_file_mb == 2.5 and cfg.ui.editor == "nvim"
    assert cfg.ui.scan_on_start
    assert cfg.ignore_paths == ["vendor/**", "docs/**"] and cfg.templates_dir == "~/tpl"
    assert cfg.checks.fail_on.value == "medium" and cfg.license == "ISC"
    assert cfg.checks.disabled == ["stashes"]
    assert cfg.allowlist == []


async def test_settings_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(tmp_path)
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        screen = app.screen
        assert app.config.mcp.restrict_to_roots and app.config.mcp.writes == []
        screen.query_one("#mcp_network").value = "on"  # type: ignore[attr-defined]
        screen.query_one("#mcp_restrict").value = "off"  # type: ignore[attr-defined]
        writes = screen.query_one("#mcp_writes")
        writes.select("sync")  # type: ignore[attr-defined]
        writes.select("allowlist")  # type: ignore[attr-defined]
        await pilot.pause()
        assert app.config.mcp.writes == ["allowlist", "sync"]
        screen.query_one("#mcp-copy-code").press()  # type: ignore[attr-defined]
        screen.query_one("#mcp-copy-desktop").press()  # type: ignore[attr-defined]
        await pilot.pause()
        writes.deselect("sync")  # type: ignore[attr-defined]
        await pilot.pause()
    cfg = ConfigStore(tmp_path / "config.toml").load()
    assert cfg.mcp.network is True and cfg.mcp.restrict_to_roots is False
    assert cfg.mcp.writes == ["allowlist"]
    assert copied[0] == "claude mcp add repo-doctor -- repo-doctor mcp"
    assert '"command": "repo-doctor"' in copied[1]


@respx.mock
@pytest.mark.usefixtures("allow_file")
async def test_remote_repos_and_clone(tmp_path: Path) -> None:
    src = fx.RepoBuilder.create(tmp_path / "srv" / "novy")
    src.write("a", "a").commit()
    target_root = tmp_path / "projekty"
    target_root.mkdir()
    (target_root / "existujici").mkdir()
    api = "https://git.example.ts.net:3000/api/v1"
    respx.get(f"{api}/users/ja/repos").respond(
        json=[
            {
                "full_name": "ja/novy",
                "private": True,
                "clone_url": str(src.path),
                "ssh_url": str(src.path),
            },
            {"full_name": "nekdo/api-gateway", "private": False},
        ]
    )
    cfg = (
        f'[[roots]]\npath = "{target_root}"\n'
        '[[forges]]\nname = "forgejo"\ntype = "forgejo"\nurl = "https://git.example.ts.net:3000"\nuser = "ja"\n'
    )
    result = sample_result()
    for r in result.repos:
        for rm in r.remotes:
            rm.owner_path = f"nekdo/{r.name}"
    app = make_app(tmp_path, config=cfg, result=result)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RemoteScreen)
        assert [r.full_name for _, r in screen.missing] == ["ja/novy"]
        await pilot.press("c")
        await pilot.pause()
        dlg = app.screen
        assert app.focused is not None and app.focused.id == "cancel"
        await pilot.press("enter")  # Zrušit → nic
        assert not (target_root / "novy").exists()
        await pilot.press("c")
        await pilot.pause()
        app.screen.query_one("#dirname").value = "existujici"  # type: ignore[attr-defined]
        await pilot.pause()
        assert "už existuje" in screen_text(app)
        app.screen.query_one("#dirname").value = "novy"  # type: ignore[attr-defined]
        await pilot.pause()
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause(0.5)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert dlg is not None
        assert (target_root / "novy" / "a").exists()
        await pilot.press("v")
        assert "lokální bez" in screen_text(app)


async def test_remote_offline(tmp_path: Path) -> None:
    app = make_app(tmp_path, offline=True)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause(0.2)
        assert "Offline" in screen_text(app)
        await pilot.press("c")


@pytest.mark.parametrize("size", [(80, 24), (60, 20), (160, 48)])
async def test_responsive(tmp_path: Path, size: tuple[int, int]) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        text = screen_text(app)
        d = dash(app)
        assert d.query_one("#sidebar").display == (size[0] >= 100)
        if size[0] < 80:
            assert "HOSTING" not in text
        if size[0] >= 90:
            assert "TEP · 30 DNÍ" in text
        for key in ("enter", "f", "1", "2", "4", "5", "question_mark"):
            await pilot.press(key)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()


async def test_wizard_guards(tmp_path: Path) -> None:
    root = tmp_path / "zz-repos"
    fx.RepoBuilder.create(root / "r")
    app = make_app(tmp_path, config=None)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        app.screen.query_one("#folders").focus()
        await pilot.press("1")  # přepínání obrazovek je v průvodci blokované
        assert isinstance(app.screen, WizardScreen)
        path = app.screen.query_one("#path")
        path.focus()
        for ch in str(tmp_path / "zz-rep"):
            await pilot.press(ch)
        await pilot.pause(0.2)
        await pilot.press("tab")  # přijme návrh cesty
        assert path.value.endswith("/zz-repos/")  # type: ignore[attr-defined]
        await pilot.press("enter")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("escape")
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("enter")  # Zrušit
        assert isinstance(app.screen, WizardScreen)
    assert not (tmp_path / "config.toml").exists()


async def test_search_resets_hidden_selection(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("slash", "z", "z", "z", "q")
        await pilot.pause()
        assert app.selected_path is None


async def test_export_asks_before_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "r.md"
    target.write_text("puvodni")
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("5")
        app.screen.query_one("#path").value = str(target)  # type: ignore[attr-defined]
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("enter")
        assert target.read_text() == "puvodni"
        await pilot.press("e", "tab", "enter")
        await pilot.pause()
    assert target.read_text().startswith("# repo-doctor")


async def test_columns_follow_panel_width(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(84, 30)) as pilot:
        await pilot.pause()
        d = dash(app)
        assert not d.query_one("#sidebar").display
        await pilot.press("b")  # přepínač levého panelu zrušen (#89): panel zůstává schovaný
        await pilot.pause()
        assert not d.query_one("#sidebar").display
        cols = d._columns()
        assert cols.name_w >= 13
        assert not cols.show_pulse
        assert "infra-notes" in screen_text(app)


async def test_wizard_keys_are_remappable(tmp_path: Path) -> None:
    root = tmp_path / "w"
    fx.RepoBuilder.create(root / "r")
    repos = sample_result().repos[:1]
    cfg = tmp_path / "config.toml"
    app = make_app(tmp_path, config=None, scanner_factory=fake_factory(repos, 0.01))
    app.config.keys = {"wizard_finish": "f2"}
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        app.set_keymap({"wizard_finish": "f2"})
        app.keymap_ids["wizard_finish"] = "f2"
        app.screen.query_one("#path").value = str(root)  # type: ignore[attr-defined]
        await pilot.press("f2")
        await pilot.pause(0.3)
        assert isinstance(app.screen, DashboardScreen)
        await app.workers.wait_for_complete()
    assert cfg.exists()


async def test_dialogs_enter_saves(tmp_path: Path) -> None:
    app = make_app(tmp_path, config="")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2", "n")
        await pilot.pause()
        app.screen.query_one("#name").value = "gh"  # type: ignore[attr-defined]
        app.screen.query_one("#source").value = "none"  # type: ignore[attr-defined]
        app.screen.query_one("#user").focus()
        await pilot.press("enter")  # Enter v poli uloží stejně jako ctrl+s
        await pilot.pause()
        assert [f.name for f in app.config.forges] == ["gh"]
    from repo_doctor.keymap import grouped

    g = grouped()
    assert ("ctrl+s", "uložit a skenovat") in g["Průvodce prvním spuštěním"]
    assert ("ctrl+s", "uložit / potvrdit formulář") in g["Formulářové dialogy"]


async def test_scan_on_start_is_opt_in(tmp_path: Path) -> None:
    repos = sample_result().repos[:2]
    app = make_app(tmp_path, auto_scan=True, scanner_factory=fake_factory(repos, 0.01))
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        assert app.scanner is None  # výchozí: jen poslední výsledek, žádný sken
    app = make_app(
        tmp_path,
        config=CONFIG + "\n[ui]\nscan_on_start = true\n",
        auto_scan=True,
        scanner_factory=fake_factory(repos, 0.01),
    )
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        assert app.scanner is not None
        await app.workers.wait_for_complete()


async def test_quit_stops_running_scan(tmp_path: Path) -> None:
    from repo_doctor import gitwrap

    repos = sample_result().repos
    app = make_app(
        tmp_path,
        result=sample_result().model_copy(update={"repos": []}),
        scanner_factory=fake_factory(repos, 0.2),
    )
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause(0.3)
        assert app.scanning
        scanner = app.scanner
        await pilot.press("ctrl+q")
    assert scanner is not None and scanner.cancelled
    assert gitwrap.aborted()
    assert app.return_code == 0


async def test_open_editor_gui_runs_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr("repo_doctor.tui.screens.detail.open_in_editor_detached", launched.append)
    result = sample_result()
    repo = next(r for r in result.repos if r.name == "infra-notes")
    repo.path = str(tmp_path / "infra-notes")
    finding = next(f for f in repo.findings if f.location.path)
    target = Path(repo.path) / str(finding.location.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n")
    app = make_app(tmp_path, config=CONFIG + '\n[ui]\neditor = "gedit"\n', result=result)
    async with app.run_test(size=(100, 31)) as pilot:
        await pilot.pause()
        app.open_detail(repo.path)
        await pilot.pause()
        detail = app.screen
        assert isinstance(detail, DetailScreen)
        detail.current = finding
        detail.action_open_editor()
        await pilot.pause()
        assert launched and launched[0][0] == "gedit" and launched[0][-1] == str(target)
        assert isinstance(app.screen, DetailScreen)  # TUI se nepozastavilo


async def test_settings_editor_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.widgets import Input, Static

    present = {"nvim", "code"}
    monkeypatch.setattr(
        "repo_doctor.tui.util.shutil.which", lambda n: f"/usr/bin/{n}" if n in present else None
    )
    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.delenv("VISUAL", raising=False)
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause()
        screen = app.screen
        info = screen.query_one("#editor-info", Static)
        assert "nano (podle: $EDITOR) – ⚠ není nainstalovaný" in str(info.render())
        custom = screen.query_one("#editor", Input)
        assert not custom.display
        choice = screen.query_one("#editor-choice")
        choice.value = "code"  # type: ignore[attr-defined]
        await pilot.pause()
        assert app.config.ui.editor == "code"
        assert str(info.render()) == "Spustí se: code (podle: Nastavení)"
        choice.value = "__custom__"  # type: ignore[attr-defined]
        await pilot.pause()
        assert custom.display and app.config.ui.editor == "code"
        custom.value = "nvim -p"
        await pilot.pause()
        assert app.config.ui.editor == "nvim -p"
        choice.value = "__auto__"
        await pilot.pause()
        assert app.config.ui.editor is None and not custom.display
    # vlastní příkaz, který není v seznamu, se po otevření ukáže jako „vlastní“
    app = make_app(tmp_path, config=CONFIG + '\n[ui]\neditor = "nvim -p"\n')
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause()
        assert app.screen.query_one("#editor-choice").value == "__custom__"
        assert app.screen.query_one("#editor", Input).value == "nvim -p"


def test_scan_dots_animate_and_stack() -> None:
    from repo_doctor.tui.widgets.vitals import scan_dots

    assert scan_dots(20, 0) != scan_dots(20, 5)
    assert scan_dots(20, 3) == scan_dots(20, 3)  # čistá funkce snímku
    assert all(len(scan_dots(w, 7)) == w for w in (10, 20))
    # v jednom sloupci jich bývá víc najednou (braille buňka nese až 8 teček)
    assert any(m & (m - 1) for f in range(60) for m in scan_dots(20, f))


def test_scan_bar_is_green_where_filled() -> None:
    from repo_doctor.tui.widgets.vitals import scan_bar

    for frame in range(40):
        bar = scan_bar(10, 5, 10, frame, PALETTE)
        assert bar.cell_len == 10
        # animace nesmí prostupovat do zeleného pruhu (#89): vyplněná část je bez teček
        assert bar.plain[:5] == " " * 5
        assert [s.style.bgcolor.name for s in bar.spans[:5]] == [PALETTE.ok] * 5  # type: ignore[union-attr]
        assert [s.style.bgcolor.name for s in bar.spans[5:]] == [PALETTE.dim] * 5  # type: ignore[union-attr]
    # v pravé části se tečky naopak objevují
    assert any(scan_bar(10, 5, 10, f, PALETTE).plain[5:].strip() for f in range(40))


async def test_dashboard_keybar_shows_settings(tmp_path: Path) -> None:
    """Nastavení je ve spodní liště přehledu (#89), i na 120 sloupcích."""
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        assert "nastavení" in screen_text(app)
