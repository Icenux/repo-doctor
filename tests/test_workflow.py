"""Workflow „vyléčit a dotáhnout do mergi“ (#70): léčba → push → PR → CI → merge, zastavení při chybě."""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from repo_doctor import workflow
from repo_doctor.config import Config
from repo_doctor.gitwrap import Git
from repo_doctor.models import FileChange, LocalAction, Patch
from tests.factory import RepoBuilder, git, with_remote
from tests.test_publish import (
    BRANCH,
    REPO_API,
    TOKEN,
    fix_repo,
    github_config,
    pr_json,
    remote_branches,
    repo_json,
)

pytestmark = pytest.mark.usefixtures("allow_file")

TODAY = date(2026, 10, 27)  # dává větev BRANCH
OK = {"name": "test", "status": "completed", "conclusion": "success", "html_url": "u1"}
RED = {"name": "lint", "status": "completed", "conclusion": "failure", "html_url": "u2"}
RUNNING = {"name": "test", "status": "in_progress", "conclusion": None, "html_url": "u1"}


async def no_sleep(seconds: float) -> None:
    return None


class Events:
    def __init__(self) -> None:
        self.items: list[workflow.Event] = []

    def __call__(self, event: workflow.Event) -> None:
        self.items.append(event)

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.items if e.kind != "waiting"]

    @property
    def last(self) -> workflow.Event:
        return self.items[-1]


def context(events: Events, **kw: Any) -> workflow.Context:
    return workflow.Context(emit=events, sleep=no_sleep, **kw)


def gitignore_patch() -> Patch:
    return Patch(
        check_id="gitignore-env",
        title="gitignore",
        summary="Přidá .env do .gitignore.",
        changes=[FileChange(path=".gitignore", action="create", content=".env\n")],
    )


class FakeGitHub:
    """GitHub, který ví, co se s PR stalo: vytvoření, průběh CI (po krocích) a merge."""

    def __init__(self, rb: RepoBuilder, ci: list[list[dict[str, Any]]], *, pr_exists: bool = False):
        self.rb = rb
        self.ci = list(ci)
        self.created = pr_exists
        self.merged = False
        self.polls = 0
        self.merge_calls = 0
        self.create_calls = 0
        respx.get(REPO_API).respond(json=repo_json())
        respx.get(f"{REPO_API}/pulls").mock(side_effect=self._list)
        respx.post(f"{REPO_API}/pulls").mock(side_effect=self._create)
        respx.get(f"{REPO_API}/pulls/7").mock(side_effect=self._detail)
        respx.get(url__regex=rf"{REPO_API}/commits/\w+/check-runs").mock(side_effect=self._checks)
        respx.get(url__regex=rf"{REPO_API}/commits/\w+/status").respond(
            json={"state": "pending", "total_count": 0}
        )
        respx.get(f"{REPO_API}/actions/runs").respond(json={"workflow_runs": []})
        respx.put(f"{REPO_API}/pulls/7/merge").mock(side_effect=self._merge)

    @property
    def sha(self) -> str:
        return self.rb.git("rev-parse", BRANCH).strip()

    def _pr(self) -> dict[str, Any]:
        return pr_json(self.sha, **({"state": "closed", "merged": True} if self.merged else {}))

    def _list(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[self._pr()] if self.created else [])

    def _create(self, request: httpx.Request) -> httpx.Response:
        self.created = True
        self.create_calls += 1
        return httpx.Response(201, json=self._pr())

    def _detail(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self._pr())

    def _checks(self, request: httpx.Request) -> httpx.Response:
        self.polls += 1
        runs = self.ci[0] if len(self.ci) == 1 else self.ci.pop(0)
        return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})

    def _merge(self, request: httpx.Request) -> httpx.Response:
        self.merged = True
        self.merge_calls += 1
        return httpx.Response(200, json={"merged": True})


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RepoBuilder, Path]:
    """Klon s remotem: hosting je „GitHub“ (jen pro API), push jde do lokálního bare repa."""
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, bare = with_remote(tmp_path)
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    rb.git("config", "remote.origin.pushurl", str(bare))
    return rb, bare


# ----------------------------------------------------------------------------- celý řetěz
@respx.mock
def test_heal_push_pr_wait_for_ci_and_merge(repo: tuple[RepoBuilder, Path]) -> None:
    rb, bare = repo
    gh = FakeGitHub(rb, [[RUNNING], [RUNNING], [OK]])
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path), github_config(), [gitignore_patch()], context(events), today=TODAY
    )
    assert events.kinds == ["healed", "pushed", "pr", "merged"]
    assert events.last.level == "ok" and events.last.url.endswith("/pull/7")
    assert remote_branches(bare) == {"main", BRANCH}
    assert gh.create_calls == 1 and gh.merge_calls == 1 and gh.polls >= 3
    assert any(e.kind == "waiting" for e in events.items)
    # pracovní strom a výchozí větev zůstaly nedotčené
    assert not (rb.path / ".gitignore").exists()


@respx.mock
def test_failed_ci_stops_the_workflow_and_never_merges(repo: tuple[RepoBuilder, Path]) -> None:
    rb, bare = repo
    gh = FakeGitHub(rb, [[RUNNING], [OK, RED]])
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path), github_config(), [gitignore_patch()], context(events), today=TODAY
    )
    assert events.kinds == ["healed", "pushed", "pr", "ci_failed"]
    assert events.last.level == "error"
    assert "lint" in events.last.text and "test" not in events.last.text
    assert events.last.url.endswith("/pull/7")
    assert gh.merge_calls == 0
    assert BRANCH in remote_branches(bare)  # větev i PR zůstaly pro ruční dokončení


@respx.mock
def test_no_ci_is_not_merged_blindly(repo: tuple[RepoBuilder, Path]) -> None:
    rb, _ = repo
    gh = FakeGitHub(rb, [[]])
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path),
        github_config(),
        [gitignore_patch()],
        context(events, poll=30, grace=90),
        today=TODAY,
    )
    assert events.last.kind == "no_ci" and events.last.level == "warn"
    assert "90 s" in events.last.text
    assert gh.merge_calls == 0 and gh.polls == 4  # 0, 30, 60, 90 s


@respx.mock
def test_ci_timeout_stops_without_merge(repo: tuple[RepoBuilder, Path]) -> None:
    rb, _ = repo
    gh = FakeGitHub(rb, [[RUNNING]])
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path),
        github_config(),
        [gitignore_patch()],
        context(events, poll=60, timeout=180),
        today=TODAY,
    )
    assert events.last.kind == "timeout" and "3 minut" in events.last.text
    assert gh.merge_calls == 0


@respx.mock
def test_merge_refused_by_hosting_is_reported(repo: tuple[RepoBuilder, Path]) -> None:
    rb, _ = repo
    FakeGitHub(rb, [[OK]])
    respx.put(f"{REPO_API}/pulls/7/merge").respond(405, json={"message": "protected"})
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path), github_config(), [gitignore_patch()], context(events), today=TODAY
    )
    assert events.last.kind == "refused" and "405" in events.last.text


@respx.mock
def test_unmergeable_pr_stops_with_the_reason(repo: tuple[RepoBuilder, Path]) -> None:
    rb, _ = repo
    FakeGitHub(rb, [[OK]])
    respx.get(f"{REPO_API}/pulls/7").mock(
        side_effect=lambda r: httpx.Response(
            200, json=pr_json(rb.git("rev-parse", BRANCH).strip(), mergeable=False)
        )
    )
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path), github_config(), [gitignore_patch()], context(events), today=TODAY
    )
    assert events.last.kind == "blocked" and "nejde mergnout" in events.last.text


# ----------------------------------------------------------------------------- předpoklady
@respx.mock
def test_hosting_is_checked_before_a_branch_is_created(
    repo: tuple[RepoBuilder, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    rb, bare = repo
    monkeypatch.delenv("RD_TEST_TOKEN")
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path), github_config(), [gitignore_patch()], context(events), today=TODAY
    )
    assert events.kinds == ["refused"] and "RD_TEST_TOKEN" in events.last.text
    assert git(rb.path, "branch", "--list", "repo-doctor/*").strip() == ""
    assert remote_branches(bare) == {"main"}


@respx.mock
def test_dirty_repo_is_refused_without_touching_anything(repo: tuple[RepoBuilder, Path]) -> None:
    rb, bare = repo
    FakeGitHub(rb, [[OK]])
    rb.write("rozdelano.txt", "x")
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path), github_config(), [gitignore_patch()], context(events), today=TODAY
    )
    assert events.kinds == ["refused"] and "necommitnuté změny" in events.last.text
    assert remote_branches(bare) == {"main"}


def test_local_only_fixes_have_nothing_to_publish(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path)
    patch = Patch(
        check_id="email-exposure",
        title="e-mail",
        summary="Nastaví e-mail.",
        local=[
            LocalAction(
                kind="git_config", key="user.email", value="a@b.cz", description="user.email"
            )
        ],
    )
    events = Events()
    workflow.heal_and_publish(Git(rb.path), Config(), [patch], context(events), today=TODAY)
    assert events.kinds == ["nothing"]
    assert rb.git("config", "--local", "user.email").strip() == "a@b.cz"


@respx.mock
def test_cancel_stops_before_anything_happens(repo: tuple[RepoBuilder, Path]) -> None:
    rb, bare = repo
    FakeGitHub(rb, [[OK]])
    cancel = threading.Event()
    cancel.set()
    events = Events()
    workflow.heal_and_publish(
        Git(rb.path),
        github_config(),
        [gitignore_patch()],
        context(events, cancel=cancel),
        today=TODAY,
    )
    assert events.kinds == ["cancelled"]
    assert remote_branches(bare) == {"main"}


@respx.mock
def test_cancel_during_ci_wait_keeps_branch_and_pr(repo: tuple[RepoBuilder, Path]) -> None:
    rb, bare = repo
    gh = FakeGitHub(rb, [[RUNNING]])
    cancel = threading.Event()

    async def cancel_on_first_wait(seconds: float) -> None:
        cancel.set()

    events = Events()
    ctx = workflow.Context(emit=events, sleep=cancel_on_first_wait, cancel=cancel)
    workflow.heal_and_publish(Git(rb.path), github_config(), [gitignore_patch()], ctx, today=TODAY)
    assert events.kinds[-1] == "cancelled" and "pr" in events.kinds
    assert gh.merge_calls == 0 and BRANCH in remote_branches(bare)


# ----------------------------------------------------------------------------- navázání
@respx.mock
def test_publish_existing_skips_steps_that_are_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)  # push už proběhl
    gh = FakeGitHub(rb, [[RUNNING], [OK]], pr_exists=True)  # PR už existuje
    events = Events()
    workflow.publish_existing(Git(rb.path), github_config(), context(events))
    assert events.kinds == ["merged"]
    assert gh.create_calls == 0 and gh.merge_calls == 1


@respx.mock
def test_publish_existing_reports_an_already_merged_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)
    gh = FakeGitHub(rb, [[OK]], pr_exists=True)
    gh.merged = True
    events = Events()
    workflow.publish_existing(Git(rb.path), github_config(), context(events))
    assert events.kinds == ["merged"] and "už je mergnutý" in events.last.text
    assert gh.merge_calls == 0


def test_publish_existing_without_fix_branch(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path)
    events = Events()
    workflow.publish_existing(Git(rb.path), Config(), context(events))
    assert events.kinds == ["refused"] and "nemá žádnou opravnou větev" in events.last.text


def test_events_know_what_is_stored_and_final() -> None:
    progress = workflow.Event("waiting", "info", "CI běží")
    assert not progress.stored and not progress.final
    assert workflow.Event("pr", "info", "x").stored
    assert workflow.Event("ci_failed", "error", "x").final
