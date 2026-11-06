"""Workflow léčby v TUI (#70): klávesy, běh na pozadí, blok Notifikace na přehledu a navigace."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from repo_doctor import notify
from repo_doctor.tui.app import RepoDoctorApp
from repo_doctor.tui.screens.dashboard import DashboardScreen, SideBar
from repo_doctor.tui.screens.dialogs import ConfirmDialog
from repo_doctor.tui.screens.fix import FixScreen
from repo_doctor.tui.screens.publish import PublishScreen
from tests import factory as fx
from tests.test_publish import BRANCH, TOKEN, fix_repo
from tests.test_publish_tui import confirm, press_ok, result_for, settle
from tests.test_tui import _messy, _result_for
from tests.test_workflow import OK, RUNNING, FakeGitHub, no_sleep
from tests.tui_helpers import CONFIG, make_app, screen_text

pytestmark = pytest.mark.usefixtures("allow_file")

SIZE = (120, 40)


@pytest.fixture(autouse=True)
def desktop(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Desktopové upozornění se zachytí (nikdy nevolá skutečné notify-send)."""
    sent: list[tuple[str, str]] = []

    def fake(title: str, text: str) -> notify.Delivery:
        sent.append((title, text))
        return notify.Delivery("desktop", True)

    monkeypatch.setattr(notify, "send_desktop", fake)
    return sent


def dash(app: RepoDoctorApp) -> DashboardScreen:
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


async def test_fix_publish_asks_first_then_reports_a_problem_in_notifications(
    tmp_path: Path, desktop: list[tuple[str, str]]
) -> None:
    repo = _messy(tmp_path)  # repo bez remotu → workflow se zastaví dřív, než cokoli vytvoří
    app = make_app(tmp_path, result=_result_for(repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await settle(app, pilot)
        assert isinstance(app.screen, FixScreen)
        await pilot.press("P")  # předvybrané jsou všechny opravy
        dialog = await confirm(app, pilot)  # výchozí volba Zrušit
        text = str(dialog.body)
        assert "push" in text and "ZELENÉ CI" in text and "nikdy force" in text
        await pilot.press("enter")  # Zrušit
        await settle(app, pilot)
        assert isinstance(app.screen, FixScreen) and not app.workflows.inbox.entries
        await pilot.press("P")
        await confirm(app, pilot)
        await press_ok(app, pilot)
        await settle(app, pilot)
        d = dash(app)  # obrazovka léčby se zavřela, workflow běželo na pozadí
        (entry,) = app.workflows.inbox.entries
        assert entry.kind == "refused" and "remote" in entry.text
        assert not app.workflows.active(str(repo))
        assert "NOTIFIKACE (1)" in screen_text(app) and "infra-notes" in screen_text(app)
        assert d.rd.workflows.inbox.unread() == 1
    assert "repo-doctor/fixes" not in fx.git(repo, "branch")
    assert desktop and "infra-notes" in desktop[0][1]  # chyba přišla i jako desktopové upozornění


async def test_fix_publish_needs_a_selection_and_online_mode(tmp_path: Path) -> None:
    repo = _messy(tmp_path)
    app = make_app(tmp_path, result=_result_for(repo), offline=True)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await settle(app, pilot)
        await pilot.press("A", "P")  # A odznačí vše → nic nevybráno
        assert isinstance(app.screen, FixScreen)
        assert any("Nic není vybráno" in n.message for n in app._notifications)
        await pilot.press("A", "P")  # offline
        assert isinstance(app.screen, FixScreen)
        assert any("offline" in n.message for n in app._notifications)


async def test_clicking_a_notification_opens_its_page_and_marks_it_read(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    app = make_app(tmp_path, result=result_for(rb.path))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        entry = app.workflows.inbox.add(
            str(rb.path), "infra-notes", "ci_failed", "error", "CI u PR #7 selhala (lint)."
        )
        app.on_inbox_changed()
        await pilot.pause()
        assert "NOTIFIKACE (1)" in screen_text(app) and "CI u PR" not in screen_text(app)
        sidebar = dash(app).query_one(SideBar)
        sidebar.focus()
        sidebar.highlighted = sidebar.get_option_index(f"n:{entry.id}")
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, PublishScreen) and app.screen.path == str(rb.path)
        assert app.workflows.inbox.unread() == 0
        await pilot.press("q")
        await pilot.pause()
        assert "NOTIFIKACE" in screen_text(app) and "NOTIFIKACE (" not in screen_text(app)


async def test_notification_of_a_repo_that_is_gone_only_warns(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        entry = app.workflows.inbox.add("/neexistuje", "ztracene", "merged", "ok", "hotovo")
        app.on_inbox_changed()
        await pilot.pause()
        dash(app)._open_notification(f"n:{entry.id}")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert any("už není ve výsledcích" in n.message for n in app._notifications)


async def test_mark_all_read_by_key_and_by_item(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for i in range(7):
            app.workflows.inbox.add(f"/r{i}", f"r{i}", "pr", "info", "x")
        app.on_inbox_changed()
        await pilot.pause()
        text = screen_text(app)
        assert "NOTIFIKACE (7)" in text and "r6" in text and "r1 " not in text  # jen posledních 5
        await pilot.press("N")
        await pilot.pause()
        assert app.workflows.inbox.unread() == 0


async def test_publish_page_finishes_the_fix_branch_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: list[tuple[str, str]]
) -> None:
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)  # push už proběhl
    config = CONFIG.replace(
        'name = "github"\ntype = "github"',
        'name = "github"\ntype = "github"\ntoken_env = "RD_TEST_TOKEN"',
    )
    with respx.mock:
        gh = FakeGitHub(rb, [[RUNNING], [OK]])
        app = make_app(tmp_path, config=config, result=result_for(rb.path))
        app.workflows.sleep = no_sleep
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("p")
            await settle(app, pilot)
            assert isinstance(app.screen, PublishScreen)
            await pilot.press("A")
            dialog = app.screen
            assert isinstance(dialog, ConfirmDialog) and "ZELENÉ CI" in str(dialog.body)
            await press_ok(app, pilot)
            await settle(app, pilot)
            await pilot.pause(0.3)
            await app.workers.wait_for_complete()
            kinds = [e.kind for e in app.workflows.inbox.entries]
            assert kinds == ["merged", "pr"]
            assert gh.merge_calls == 1 and gh.create_calls == 1
            assert not app.workflows.active(str(rb.path))
    assert desktop == []  # úspěch neruší desktopovým upozorněním


async def test_only_one_workflow_per_repo(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        path = "/nekde/repo"
        app.workflows.live[path] = "čekám na CI"
        assert app.workflows.start_publish(path) is False
        await pilot.pause()
        assert any("už běží" in n.message for n in app._notifications)
        app.on_inbox_changed()
        await pilot.pause()
        assert "NOTIFIKACE" in screen_text(app) and "⟳" in screen_text(app)
