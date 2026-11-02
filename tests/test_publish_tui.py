"""Odeslání oprav v TUI: klávesa → náhled v dialogu (výchozí „Zrušit“) → provedení."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from repo_doctor.gitwrap import Git
from repo_doctor.tui.app import RepoDoctorApp
from repo_doctor.tui.screens.detail import DetailScreen
from repo_doctor.tui.screens.dialogs import ConfirmDialog
from repo_doctor.tui.screens.publish import PublishScreen
from repo_doctor.tui.widgets.pipeline import PipelineView
from tests.factory import RepoBuilder, git
from tests.sample import sample_result
from tests.test_publish import (
    BRANCH,
    GREEN,
    REPO_API,
    SUBJECT,
    TOKEN,
    fix_repo,
    mock_pr_state,
    pr_json,
    remote_branches,
    repo_json,
)
from tests.tui_helpers import CONFIG, make_app, screen_text

pytestmark = pytest.mark.usefixtures("allow_file")


def result_for(path: Path) -> Any:
    result = sample_result()
    result.repos[0].path = str(path)
    return result


async def settle(app: RepoDoctorApp, pilot: Any) -> None:
    await pilot.pause(0.3)
    await app.workers.wait_for_complete()
    await pilot.pause()


async def open_detail(app: RepoDoctorApp, pilot: Any) -> PublishScreen:
    """Karta repa → stránka Odeslání oprav (klávesa p); počká na načtení stavu."""
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, DetailScreen)
    await pilot.press("p")
    await settle(app, pilot)
    assert isinstance(app.screen, PublishScreen)
    return app.screen


async def confirm(app: RepoDoctorApp, pilot: Any) -> ConfirmDialog:
    """Počká na dialog; ověří, že výchozí volba je Zrušit."""
    await settle(app, pilot)
    dialog = app.screen
    assert isinstance(dialog, ConfirmDialog)
    assert app.focused is not None and app.focused.id == "cancel"
    return dialog


async def press_ok(app: RepoDoctorApp, pilot: Any) -> None:
    await pilot.press("tab", "enter")
    await settle(app, pilot)


async def test_push_cancel_then_confirm(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    app = make_app(tmp_path, result=result_for(rb.path))
    async with app.run_test(size=(120, 40)) as pilot:
        await open_detail(app, pilot)
        await pilot.press("P")
        dialog = await confirm(app, pilot)
        assert BRANCH in str(dialog.body) and SUBJECT in str(dialog.body)
        await pilot.press("enter")  # Zrušit
        await settle(app, pilot)
        assert isinstance(app.screen, PublishScreen)
        assert remote_branches(bare) == {"main"}
        await pilot.press("P")
        await confirm(app, pilot)
        await press_ok(app, pilot)
    assert remote_branches(bare) == {"main", BRANCH}


async def test_refusal_is_only_a_notification(tmp_path: Path) -> None:
    rb, _ = with_no_fix_branch(tmp_path)
    app = make_app(tmp_path, result=result_for(rb.path))
    async with app.run_test(size=(120, 40)) as pilot:
        await open_detail(app, pilot)
        await pilot.press("P")
        await settle(app, pilot)
        assert isinstance(app.screen, PublishScreen)  # žádný dialog
        assert any("nemá žádnou opravnou větev" in n.message for n in app._notifications)


def with_no_fix_branch(tmp_path: Path) -> tuple[RepoBuilder, Path]:
    from tests.factory import with_remote

    return with_remote(tmp_path)


async def test_local_merge_then_push_default(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    app = make_app(tmp_path, result=result_for(rb.path))
    async with app.run_test(size=(120, 40)) as pilot:
        await open_detail(app, pilot)
        await pilot.press("L")
        dialog = await confirm(app, pilot)
        assert "fast-forward" in str(dialog.body) and "Nic se nepushne" in str(dialog.body)
        await press_ok(app, pilot)
        assert rb.git("rev-parse", "main").strip() == rb.git("rev-parse", BRANCH).strip()
        await pilot.press("U")
        await confirm(app, pilot)
        await press_ok(app, pilot)
    assert git(bare, "rev-parse", "main").strip() == rb.git("rev-parse", "main").strip()


@respx.mock
async def test_create_pr_and_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)
    config = CONFIG.replace(
        'name = "github"\ntype = "github"',
        'name = "github"\ntype = "github"\ntoken_env = "RD_TEST_TOKEN"',
    )
    respx.get(REPO_API).respond(json=repo_json())
    respx.get(f"{REPO_API}/pulls").respond(json=[])
    created = respx.post(f"{REPO_API}/pulls").respond(201, json=pr_json(sha))
    app = make_app(tmp_path, config=config, result=result_for(rb.path))
    async with app.run_test(size=(120, 40)) as pilot:
        await open_detail(app, pilot)
        await pilot.press("C")
        dialog = await confirm(app, pilot)
        assert "github" in str(dialog.body) and SUBJECT in str(dialog.body)
        await press_ok(app, pilot)
        assert created.called
        # merge: PR už existuje, CI běží → jen upozornění; po zelené CI dialog
        mock_pr_state(sha, checks=[{"status": "in_progress", "conclusion": None}])
        merged = respx.put(f"{REPO_API}/pulls/7/merge").respond(200, json={"merged": True})
        await pilot.press("M")
        await settle(app, pilot)
        assert isinstance(app.screen, PublishScreen)
        assert any("CI ještě běží" in n.message for n in app._notifications)
        assert not merged.called
        detail = mock_pr_state(sha, checks=GREEN)
        # náhled a kontrola před zápisem vidí otevřený PR, teprve po merge je mergnutý
        detail.side_effect = [
            httpx.Response(200, json=pr_json(sha)),
            httpx.Response(200, json=pr_json(sha)),
            *[httpx.Response(200, json=pr_json(sha, merged=True, state="closed"))] * 3,
        ]
        await pilot.press("M")
        await confirm(app, pilot)
        await press_ok(app, pilot)
        assert merged.called


def green_and_red(sha: str, *, pending: bool = False) -> None:
    mock_pr_state(
        sha,
        checks=[
            {"name": "test", "status": "completed", "conclusion": "success", "html_url": "u1"},
            {
                "name": "lint",
                "status": "in_progress" if pending else "completed",
                "conclusion": None if pending else "failure",
                "html_url": "https://github.com/nekdo/proj/runs/2",
            },
        ],
    )


def hosted_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: str | None = None
) -> tuple[RepoDoctorApp, str]:
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    if workflow is not None:  # workflow je commitnutý v opravné větvi, odkud se čtou `needs`
        rb.git("checkout", "-q", BRANCH)
        rb.write(".github/workflows/ci.yml", workflow).commit("ci: workflow")
        rb.git("checkout", "-q", "main")
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)
    config = CONFIG.replace(
        'name = "github"\ntype = "github"',
        'name = "github"\ntype = "github"\ntoken_env = "RD_TEST_TOKEN"',
    )
    respx.get(REPO_API).respond(json=repo_json())
    return make_app(tmp_path, config=config, result=result_for(rb.path)), sha


@respx.mock
async def test_page_shows_pr_ci_checks_and_why_merge_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sha = hosted_app(tmp_path, monkeypatch)
    green_and_red(sha)
    async with app.run_test(size=(140, 40)) as pilot:
        await open_detail(app, pilot)
        text = screen_text(app)
        assert BRANCH in text and "#7 otevřený" in text
        assert "selhala – 1 z 2 kontrol" in text
        assert "test" in text and "lint" in text
        assert "CI selhala" in text  # důvod, proč merge nejde
        # klávesa M dá jen upozornění, žádný dialog
        await pilot.press("M")
        await settle(app, pilot)
        assert isinstance(app.screen, PublishScreen)


@respx.mock
async def test_page_refreshes_itself_while_ci_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sha = hosted_app(tmp_path, monkeypatch)
    green_and_red(sha, pending=True)
    async with app.run_test(size=(140, 40)) as pilot:
        page = await open_detail(app, pilot)
        assert page.status is not None and page.status.ci is not None
        assert page.status.ci.state == "pending" and page._timer is not None
        assert "běží – 1 z 2 kontrol ještě neskončilo" in screen_text(app)
        mock_pr_state(sha, checks=GREEN)
        await pilot.press("ctrl+r")  # ruční obnovení, bez čekání na časovač
        await settle(app, pilot)
        assert page.status.ci is not None and str(page.status.ci.state) == "success"
        assert page._timer is None and "jde mergnout" in screen_text(app)


async def test_page_works_without_hosting_and_names_the_next_step(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    app = make_app(tmp_path, result=result_for(rb.path))
    async with app.run_test(size=(140, 40)) as pilot:
        await open_detail(app, pilot)
        text = screen_text(app)
        assert "na origin zatím není" in text and "nepatří žádnému nastavenému hostingu" in text
        await pilot.press("escape")
        assert isinstance(app.screen, DetailScreen)


WORKFLOW = """
name: CI
jobs:
  lint: {runs-on: x}
  test: {needs: lint}
  build: {needs: [test]}
"""


def mock_jobs(sha: str) -> None:
    mock_pr_state(sha, checks=[{"name": "lint", "status": "completed", "conclusion": "success"}])
    respx.get(f"{REPO_API}/actions/runs").respond(
        json={"workflow_runs": [{"id": 5, "name": "CI", "path": ".github/workflows/ci.yml"}]}
    )
    steps = [
        {"name": "Set up job", "number": 1, "status": "completed", "conclusion": "success"},
        {"name": "Run pytest", "number": 2, "status": "in_progress", "conclusion": None},
    ]
    respx.get(f"{REPO_API}/actions/runs/5/jobs").respond(
        json={
            "jobs": [
                {"name": "lint", "status": "completed", "conclusion": "success",
                 "html_url": "https://github.com/nekdo/proj/job/1"},
                {"name": "test", "status": "in_progress", "conclusion": None, "steps": steps,
                 "html_url": "https://github.com/nekdo/proj/job/2"},
                {"name": "build", "status": "queued", "conclusion": None},
            ]
        }
    )  # fmt: skip


@respx.mock
async def test_pipeline_view_shows_jobs_dependencies_and_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sha = hosted_app(tmp_path, monkeypatch, WORKFLOW)
    sha = Git(tmp_path / "infra-notes").rev_parse(f"refs/heads/{BRANCH}") or sha
    mock_jobs(sha)
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr("repo_doctor.tui.screens.publish.open_url", fake_open)
    async with app.run_test(size=(140, 40)) as pilot:
        page = await open_detail(app, pilot)
        await pilot.press("v")
        await settle(app, pilot)
        text = screen_text(app)
        assert page.show_pipeline and "lint" in text and "build" in text and "▶" in text
        assert "odkaz" not in text and "Kontroly CI" not in text  # tabulka je schovaná
        view = page.query_one(PipelineView)
        graph = view.graphs[0]
        assert graph.title == "CI" and [[graph.jobs[i].name for i in c] for c in graph.columns] == [
            ["lint"], ["test"], ["build"],
        ]  # fmt: skip
        assert view.selected_job is not None and view.selected_job.name == "lint"
        await pilot.press("right")
        assert view.selected_job is not None and view.selected_job.name == "test"
        assert "Run pytest" not in screen_text(app)
        await pilot.press("enter")  # rozbalí kroky
        await pilot.pause()
        assert "Run pytest" in screen_text(app) and "Set up job" in screen_text(app)
        await pilot.press("w")
        assert opened == ["https://github.com/nekdo/proj/job/2"]
        await pilot.press("v")
        assert not page.show_pipeline and "odkaz" in screen_text(app)


def test_duration_and_fit_helpers() -> None:
    from datetime import UTC, datetime

    from repo_doctor.tui.widgets.pipeline import duration, elapsed, fit

    assert [duration(x) for x in (-3, 7, 75, 3725)] == ["0s", "7s", "1m 15s", "1h 02m"]
    t0 = datetime(2026, 11, 2, 10, tzinfo=UTC)
    assert elapsed(None, None, t0) == "" and elapsed(t0, t0.replace(minute=2), t0) == "2m 00s"
    assert fit("abcdef", 4) == "abc…" and fit("ab", 4) == "ab"


@respx.mock
async def test_pipeline_view_stages_long_edges_and_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repo_doctor.forges.base import CIJob, CIStep

    app, sha = hosted_app(tmp_path, monkeypatch)
    mock_pr_state(sha, checks=GREEN)
    async with app.run_test(size=(140, 40)) as pilot:
        page = await open_detail(app, pilot)
        await pilot.press("v")
        view = page.query_one(PipelineView)
        steps = [CIStep("krok A", "success"), CIStep("krok B", "cancelled")]
        view.set_jobs(
            [
                CIJob("a", "success", workflow="Pipeline #1", stage="check"),
                CIJob("b", "cancelled", workflow="Pipeline #1", stage="test", steps=steps),
                CIJob("c", "skipped", workflow="Pipeline #1", stage="test"),
                CIJob("d", "failure", workflow="Pipeline #1", stage="ship"),
                CIJob("e", "queued", workflow="Gitea", needs=["f", "x"]),
                CIJob("f", "running", workflow="Gitea", needs=["g"]),
                CIJob("g", "success", workflow="Gitea"),
                CIJob("h", "success", workflow="Gitea", needs=["e", "g"]),
            ]
        )
        await pilot.pause()
        text = screen_text(app)
        assert "check" in text and "ship" in text and "Pipeline #1" in text
        for key in ("j", "j", "right", "left", "down", "up", "k"):
            await pilot.press(key)
        assert view.selected_job is not None
        view.selected = (0, 1)
        await pilot.press("enter")  # job s kroky se rozbalí
        await pilot.pause()
        assert "krok B" in screen_text(app)
        view.selected = (0, 0)
        await pilot.press("enter")  # job bez kroků se nerozbaluje
        await pilot.press("escape")
        assert isinstance(app.screen, DetailScreen)
