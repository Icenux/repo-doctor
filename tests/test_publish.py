"""Odeslání oprav: push, lokální merge, PR a merge na hostingu (vše offline)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
import respx

from repo_doctor import publish
from repo_doctor.config import Config, ForgeConfig, PublishConfig
from repo_doctor.gitwrap import Git
from repo_doctor.publish import PublishRefused
from tests.factory import RepoBuilder, fake_aws_key, git, with_remote

pytestmark = pytest.mark.usefixtures("allow_file")

BRANCH = "repo-doctor/fixes-2026-10-27"
SUBJECT = "chore(repo-doctor): gitignore-env"


def fix_repo(tmp_path: Path, *, secret: bool = False) -> tuple[RepoBuilder, Path]:
    """Klon z bare remotu s opravnou větví (jeden commit); HEAD zůstává na main."""
    rb, bare = with_remote(tmp_path)
    rb.git("checkout", "-q", "-b", BRANCH)
    content = f"KEY={fake_aws_key()}\n" if secret else ".env\n"
    rb.write(".gitignore" if not secret else "oops.txt", content)
    rb.commit(f"{SUBJECT}\n\nPřidá .env do .gitignore.")
    rb.git("checkout", "-q", "main")
    return rb, bare


def remote_branches(bare: Path) -> set[str]:
    return set(git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


# ----------------------------------------------------------------------------- push
def test_push_sends_only_the_fix_branch(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    main_before = rb.git("rev-parse", "main").strip()
    plan = publish.plan_push(Git(rb.path), Config())
    assert plan.branch == BRANCH and plan.remote == "origin"
    assert [c.subject for c in plan.commits] == [SUBJECT]
    publish.apply_push(Git(rb.path), plan, Config())
    assert remote_branches(bare) == {"main", BRANCH}
    assert git(bare, "rev-parse", BRANCH).strip() == rb.git("rev-parse", BRANCH).strip()
    assert git(bare, "rev-parse", "main").strip() == main_before
    # remote-tracking ref se aktualizuje, takže další náhled už nemá co poslat
    with pytest.raises(PublishRefused, match="Není co poslat"):
        publish.plan_push(Git(rb.path), Config())


def test_push_never_runs_hooks(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    marker = tmp_path / "HOOK"
    hook = rb.path / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    publish.apply_push(Git(rb.path), publish.plan_push(Git(rb.path), Config()), Config())
    assert not marker.exists()


def test_push_blocked_by_secret_in_new_commit(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path, secret=True)
    with pytest.raises(PublishRefused, match="tajemství"):
        publish.plan_push(Git(rb.path), Config())
    assert remote_branches(bare) == {"main"}


def test_push_refuses_other_branches(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    rb.git("branch", "feature")
    with pytest.raises(PublishRefused, match="jen opravnou větev"):
        publish.plan_push(Git(rb.path), Config(), "feature")
    with pytest.raises(PublishRefused, match="jen opravnou větev"):
        publish.plan_push(Git(rb.path), Config(), "-evil")
    assert remote_branches(bare) == {"main"}


def test_push_without_fix_branch_or_remote(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path)
    with pytest.raises(PublishRefused, match="nemá žádnou opravnou větev"):
        publish.plan_push(Git(rb.path), Config())
    lone = RepoBuilder.create(tmp_path / "lone")
    lone.write("a", "a").commit("init")
    lone.git("branch", BRANCH)
    with pytest.raises(PublishRefused, match="nemá žádný remote"):
        publish.plan_push(Git(lone.path), Config())


def test_push_refuses_changed_remote_history(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    publish.apply_push(Git(rb.path), publish.plan_push(Git(rb.path), Config()), Config())
    # remote má na větvi jiný commit než lokální (někdo ji přepsal) – nefetchnuto
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(bare), str(other))
    git(other, "checkout", "-q", BRANCH)
    git(other, "commit", "-q", "--allow-empty", "-m", "cizí")
    # lokální větev přepíšeme na jiný commit a tracking ref necháme na odeslaném
    rb.git("checkout", "-q", BRANCH)
    rb.git("commit", "-q", "--amend", "-m", "přepsáno")
    rb.git("checkout", "-q", "main")
    with pytest.raises(PublishRefused, match="liší od lokální"):
        publish.plan_push(Git(rb.path), Config())


def test_push_apply_detects_branch_changed_since_preview(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    plan = publish.plan_push(Git(rb.path), Config())
    rb.git("checkout", "-q", BRANCH)
    rb.write("dalsi.txt", "x").commit("další")
    rb.git("checkout", "-q", "main")
    with pytest.raises(PublishRefused, match="od náhledu změnila"):
        publish.apply_push(Git(rb.path), plan, Config())
    assert remote_branches(bare) == {"main"}


def test_push_remote_choice(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    rb.git("remote", "rename", "origin", "zdroj")
    rb.remote("zaloha", str(bare))
    # zdroj sleduje výchozí větev → vybere se
    assert publish.pick_remote(Git(rb.path), Config()) == "zdroj"
    rb.git("config", "--unset", "branch.main.remote")
    with pytest.raises(PublishRefused, match="víc remotů"):
        publish.pick_remote(Git(rb.path), Config())
    config = Config(publish=PublishConfig(remote="zaloha"))
    assert publish.pick_remote(Git(rb.path), config) == "zaloha"
    with pytest.raises(PublishRefused, match="v repu není"):
        publish.pick_remote(Git(rb.path), Config(publish=PublishConfig(remote="nikde")))


def test_push_default_branch_after_local_merge(tmp_path: Path) -> None:
    rb, bare = fix_repo(tmp_path)
    git_ = Git(rb.path)
    with pytest.raises(PublishRefused, match="Není co poslat"):
        publish.plan_push(git_, Config(), default=True)
    publish.apply_merge_local(git_, Config(), publish.plan_merge_local(git_, Config()))
    plan = publish.plan_push(git_, Config(), default=True)
    assert plan.branch == "main" and plan.is_default
    publish.apply_push(git_, plan, Config())
    assert git(bare, "rev-parse", "main").strip() == rb.git("rev-parse", "main").strip()


def test_pushed_ref_is_not_forced(tmp_path: Path) -> None:
    """Git sám odmítne non-fast-forward; repo-doctor nikdy nepoužije force."""
    rb, bare = fix_repo(tmp_path)
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(bare), str(other))
    git(other, "checkout", "-q", "-b", BRANCH)
    git(other, "commit", "-q", "--allow-empty", "-m", "cizí")
    git(other, "push", "-q", "origin", BRANCH)
    plan = publish.plan_push(Git(rb.path), Config())
    with pytest.raises(PublishRefused, match="Push selhal"):
        publish.apply_push(Git(rb.path), plan, Config())
    assert git(bare, "log", "-1", "--format=%s", BRANCH).strip() == "cizí"


# ----------------------------------------------------------------------------- lokální merge
def test_local_merge_moves_ref_without_touching_worktree(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    rb.git("checkout", "-q", "-b", "prace")  # výchozí větev není checkoutnutá
    (rb.path / "moje.txt").write_text("rozdělané\n")
    before = rb.snapshot()
    git_ = Git(rb.path)
    plan = publish.plan_merge_local(git_, Config())
    assert not plan.checked_out and plan.target == "main"
    assert plan.files == [".gitignore"]
    publish.apply_merge_local(git_, Config(), plan)
    assert rb.git("rev-parse", "main").strip() == rb.git("rev-parse", BRANCH).strip()
    after = rb.snapshot()
    refs_changed = {k for k in before if before[k] != after[k]}
    assert refs_changed == {"refs"}  # jen ref – HEAD, index, soubory a stash beze změny


def test_local_merge_updates_checked_out_clean_default_branch(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    git_ = Git(rb.path)
    plan = publish.plan_merge_local(git_, Config())
    assert plan.checked_out
    publish.apply_merge_local(git_, Config(), plan)
    assert (rb.path / ".gitignore").read_text() == ".env\n"
    assert rb.git("rev-list", "--merges", "--count", "main").strip() == "0"  # žádný merge commit
    with pytest.raises(PublishRefused, match="už ve main je"):
        publish.plan_merge_local(git_, Config())


def test_local_merge_refuses_dirty_checked_out_repo(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    (rb.path / "README.md").write_text("změněno\n")
    with pytest.raises(PublishRefused, match="necommitnuté změny"):
        publish.plan_merge_local(Git(rb.path), Config())


def test_local_merge_refuses_overwriting_untracked_file(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    (rb.path / ".gitignore").write_text("moje\n")  # nesledovaný soubor na cestě změny
    with pytest.raises(PublishRefused, match="přepsaly"):
        publish.plan_merge_local(Git(rb.path), Config())


def test_local_merge_refuses_diverged_history(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    rb.write("novinka.txt", "n").commit("main povyrostl")
    with pytest.raises(PublishRefused, match="povyrostla"):
        publish.plan_merge_local(Git(rb.path), Config())


def test_local_merge_refuses_default_branch_in_other_worktree(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    rb.git("checkout", "-q", "-b", "prace")
    rb.git("worktree", "add", "-q", str(tmp_path / "wt"), "main")
    with pytest.raises(PublishRefused, match="jiném pracovním stromě"):
        publish.plan_merge_local(Git(rb.path), Config())


def test_local_merge_works_without_remote(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "lone")
    rb.write("a", "a").commit("init")
    rb.git("checkout", "-q", "-b", BRANCH)
    rb.write("b", "b").commit("oprava")
    rb.git("checkout", "-q", "main")
    git_ = Git(rb.path)
    publish.apply_merge_local(git_, Config(), publish.plan_merge_local(git_, Config()))
    assert (rb.path / "b").exists()


def test_local_merge_apply_detects_change_since_preview(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    git_ = Git(rb.path)
    plan = publish.plan_merge_local(git_, Config())
    rb.git("checkout", "-q", BRANCH)
    rb.write("x.txt", "x").commit("po náhledu")
    rb.git("checkout", "-q", "main")
    with pytest.raises(PublishRefused, match="od náhledu změnily"):
        publish.apply_merge_local(git_, Config(), plan)


# ----------------------------------------------------------------------------- hosting
TOKEN = "S3cr3tT0k3n" * 4
API = "https://api.github.com"
REPO_API = f"{API}/repos/nekdo/proj"


def github_config(**kw: Any) -> Config:
    return Config(
        forges=[ForgeConfig(name="github", type="github", token_env="RD_TEST_TOKEN")],
        **kw,
    )


@pytest.fixture
def hosted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RepoBuilder, str]:
    """Opravná větev na „GitHubu“: remote-tracking refy stojí, jako by push proběhl."""
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)
    return rb, sha


def repo_json(**kw: Any) -> dict[str, Any]:
    return {
        "full_name": "nekdo/proj",
        "default_branch": "main",
        "private": True,
        "html_url": "https://github.com/nekdo/proj",
        "allow_merge_commit": True,
        "allow_squash_merge": True,
        "allow_rebase_merge": False,
    } | kw


def pr_json(sha: str, **kw: Any) -> dict[str, Any]:
    return {
        "number": 7,
        "html_url": "https://github.com/nekdo/proj/pull/7",
        "state": "open",
        "title": SUBJECT,
        "head": {"ref": BRANCH, "sha": sha},
        "base": {"ref": "main"},
        "mergeable": True,
    } | kw


async def connect(rb: RepoBuilder, config: Config | None = None) -> publish.Hosting:
    return await publish.connect(Git(rb.path), config or github_config(), "origin")


@respx.mock
async def test_connect_requires_token_and_known_forge(
    hosted: tuple[RepoBuilder, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rb, _ = hosted
    with pytest.raises(PublishRefused, match="nepatří žádnému nastavenému hostingu"):
        await connect(rb, Config())
    no_token = Config(forges=[ForgeConfig(name="github", type="github")])
    with pytest.raises(PublishRefused, match="nemá token"):
        await connect(rb, no_token)
    monkeypatch.delenv("RD_TEST_TOKEN")
    with pytest.raises(PublishRefused, match="RD_TEST_TOKEN"):
        await connect(rb)


@respx.mock
async def test_connect_refuses_archived_repo(hosted: tuple[RepoBuilder, str]) -> None:
    respx.get(REPO_API).respond(json=repo_json(archived=True))
    with pytest.raises(PublishRefused, match="archivované"):
        await connect(hosted[0])


@respx.mock
async def test_create_pr(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    respx.get(f"{REPO_API}/pulls").respond(json=[])
    created = respx.post(f"{REPO_API}/pulls").respond(201, json=pr_json(sha))
    hosting = await connect(rb)
    try:
        plan = await publish.plan_pr(Git(rb.path), hosting, Config())
        assert plan.title == SUBJECT and plan.base == "main" and plan.branch == BRANCH
        assert "Přidá .env do .gitignore." in plan.body
        pr = await publish.apply_pr(Git(rb.path), hosting, Config(), plan)
    finally:
        await hosting.aclose()
    assert pr.number == 7 and pr.url.endswith("/pull/7")
    request = created.calls.last.request
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "title": SUBJECT,
        "head": BRANCH,
        "base": "main",
        "body": plan.body,
    }
    assert TOKEN not in plan.body


@respx.mock
async def test_create_pr_refusals(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    listing = respx.get(f"{REPO_API}/pulls")
    hosting = await connect(rb)
    try:
        listing.respond(json=[pr_json(sha)])
        with pytest.raises(PublishRefused, match="PR už existuje: #7"):
            await publish.plan_pr(Git(rb.path), hosting, Config())
        listing.respond(json=[pr_json(sha, state="closed", merged_at="2026-10-27T10:00:00Z")])
        with pytest.raises(PublishRefused, match="už je mergnutý"):
            await publish.plan_pr(Git(rb.path), hosting, Config())
        # větev není na remote v aktuální podobě → nejdřív push
        rb.git("update-ref", "-d", f"refs/remotes/origin/{BRANCH}")
        with pytest.raises(PublishRefused, match="nejdřív ji pošli"):
            await publish.plan_pr(Git(rb.path), hosting, Config())
    finally:
        await hosting.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("status", "text"),
    [(403, "právo zápisu"), (401, "odmítl token"), (422, r"Hosting odmítl \(422\).*x-zprava")],
)
async def test_create_pr_http_errors_are_explained(
    hosted: tuple[RepoBuilder, str], status: int, text: str
) -> None:
    rb, _ = hosted
    respx.get(REPO_API).respond(json=repo_json())
    respx.get(f"{REPO_API}/pulls").respond(json=[])
    respx.post(f"{REPO_API}/pulls").respond(status, json={"message": "x-zprava"})
    hosting = await connect(rb)
    try:
        plan = await publish.plan_pr(Git(rb.path), hosting, Config())
        with pytest.raises(PublishRefused, match=text):
            await publish.apply_pr(Git(rb.path), hosting, Config(), plan)
    finally:
        await hosting.aclose()


def mock_pr_state(
    sha: str, *, mergeable: bool | None = True, checks: list[dict[str, Any]] | None = None
) -> respx.Route:
    """Namockuje stav PR #7; vrací routu jeho detailu (pro další odpovědi)."""
    respx.get(f"{REPO_API}/pulls").respond(json=[pr_json(sha)])
    detail = respx.get(f"{REPO_API}/pulls/7").respond(json=pr_json(sha, mergeable=mergeable))
    respx.get(f"{REPO_API}/commits/{sha}/check-runs").respond(
        json={"total_count": len(checks or []), "check_runs": checks or []}
    )
    respx.get(f"{REPO_API}/commits/{sha}/status").respond(
        json={"state": "pending", "total_count": 0}
    )
    respx.get(f"{REPO_API}/actions/runs").respond(json={"workflow_runs": []})
    return detail


GREEN = [{"status": "completed", "conclusion": "success"}]


@respx.mock
async def test_merge_after_green_ci(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    mock_pr_state(sha, checks=GREEN)
    merged = respx.put(f"{REPO_API}/pulls/7/merge").respond(200, json={"merged": True})
    hosting = await connect(rb)
    try:
        plan = await publish.plan_merge(Git(rb.path), hosting, Config())
        assert plan.ready and plan.method == "merge" and plan.ci.state == "success"
        await publish.apply_merge(Git(rb.path), hosting, Config(), plan)
    finally:
        await hosting.aclose()
    assert json.loads(merged.calls.last.request.content) == {"sha": sha, "merge_method": "merge"}


@respx.mock
async def test_merge_method_from_config_and_repo(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json(allow_merge_commit=False))
    mock_pr_state(sha)
    hosting = await connect(rb)
    try:
        auto = await publish.plan_merge(Git(rb.path), hosting, Config())
        rebase = await publish.plan_merge(
            Git(rb.path), hosting, Config(publish=PublishConfig(merge_method="rebase"))
        )
    finally:
        await hosting.aclose()
    assert auto.method == "squash"  # merge je v repu zakázaný → první povolená
    assert rebase.method == "rebase"


@respx.mock
@pytest.mark.parametrize(
    ("checks", "mergeable", "blocker"),
    [
        ([{"status": "in_progress", "conclusion": None}], True, "CI ještě běží"),
        ([{"status": "completed", "conclusion": "failure"}], True, "CI selhala"),
        (GREEN, False, "nejde mergnout"),
        (GREEN, None, "zjišťuje"),
    ],
)
async def test_merge_blocked(
    hosted: tuple[RepoBuilder, str],
    checks: list[dict[str, Any]],
    mergeable: bool | None,
    blocker: str,
) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    mock_pr_state(sha, mergeable=mergeable, checks=checks)
    merged = respx.put(f"{REPO_API}/pulls/7/merge").respond(200, json={})
    hosting = await connect(rb)
    try:
        plan = await publish.plan_merge(Git(rb.path), hosting, Config())
        assert not plan.ready and blocker in " ".join(plan.blockers)
        with pytest.raises(PublishRefused, match=blocker):
            await publish.apply_merge(Git(rb.path), hosting, Config(), plan)
    finally:
        await hosting.aclose()
    assert not merged.called


@respx.mock
async def test_merge_without_ci_is_allowed(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    mock_pr_state(sha, checks=[])
    hosting = await connect(rb)
    try:
        plan = await publish.plan_merge(Git(rb.path), hosting, Config())
    finally:
        await hosting.aclose()
    assert plan.ready and plan.ci.state == "none"


@respx.mock
async def test_merge_refuses_closed_missing_or_stale_pr(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    hosting = await connect(rb)
    try:
        respx.get(f"{REPO_API}/pulls").respond(json=[])
        with pytest.raises(PublishRefused, match="není žádný PR"):
            await publish.plan_merge(Git(rb.path), hosting, Config())
        mock_pr_state(sha, checks=GREEN)
        respx.get(f"{REPO_API}/pulls/7").respond(json=pr_json(sha, state="closed"))
        with pytest.raises(PublishRefused, match="je zavřený"):
            await publish.plan_merge(Git(rb.path), hosting, Config())
        respx.get(f"{REPO_API}/pulls/7").respond(json=pr_json("f" * 40))
        respx.get(f"{REPO_API}/commits/{'f' * 40}/check-runs").respond(
            json={"total_count": 1, "check_runs": GREEN}
        )
        respx.get(f"{REPO_API}/commits/{'f' * 40}/status").respond(
            json={"state": "success", "total_count": 0}
        )
        plan = await publish.plan_merge(Git(rb.path), hosting, Config())
        assert "liší od lokální" in " ".join(plan.blockers)
    finally:
        await hosting.aclose()


@respx.mock
async def test_merge_error_is_explained(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    mock_pr_state(sha, checks=GREEN)
    respx.put(f"{REPO_API}/pulls/7/merge").respond(405, json={"message": "protected"})
    hosting = await connect(rb)
    try:
        plan = await publish.plan_merge(Git(rb.path), hosting, Config())
        with pytest.raises(PublishRefused, match=r"Hosting odmítl \(405\)"):
            await publish.apply_merge(Git(rb.path), hosting, Config(), plan)
    finally:
        await hosting.aclose()


# ----------------------------------------------------------------------------- Forgejo, GitLab
@pytest.fixture
def forgejo_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RepoBuilder, str]:
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    rb.git("remote", "set-url", "origin", "https://git.example.org/nekdo/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)
    return rb, sha


@respx.mock
async def test_forgejo_create_and_merge(forgejo_repo: tuple[RepoBuilder, str]) -> None:
    rb, sha = forgejo_repo
    api = "https://git.example.org/api/v1/repos/nekdo/proj"
    config = Config(
        forges=[
            ForgeConfig(
                name="home",
                type="forgejo",
                url="https://git.example.org",
                token_env="RD_TEST_TOKEN",
            )
        ]
    )
    respx.get(api).respond(
        json={
            "full_name": "nekdo/proj",
            "default_branch": "main",
            "allow_merge_commits": False,
            "allow_squash_merge": True,
        }
    )
    pr = {
        "number": 3,
        "html_url": "https://git.example.org/nekdo/proj/pulls/3",
        "state": "open",
        "merged": False,
        "mergeable": True,
        "title": SUBJECT,
        "head": {"ref": BRANCH, "sha": sha},
        "base": {"ref": "main"},
    }
    respx.get(f"{api}/pulls").respond(json=[])
    created = respx.post(f"{api}/pulls").respond(201, json=pr)
    hosting = await publish.connect(Git(rb.path), config, "origin")
    try:
        plan = await publish.plan_pr(Git(rb.path), hosting, Config())
        info = await publish.apply_pr(Git(rb.path), hosting, Config(), plan)
        assert info.number == 3
        assert created.calls.last.request.headers["authorization"] == f"token {TOKEN}"
        respx.get(f"{api}/pulls").respond(json=[pr])
        respx.get(f"{api}/pulls/3").respond(json=pr)
        respx.get(f"{api}/commits/{sha}/status").respond(
            json={"state": "success", "total_count": 1, "statuses": [{"target_url": ""}]}
        )
        merged = respx.post(f"{api}/pulls/3/merge").respond(200)
        merge = await publish.plan_merge(Git(rb.path), hosting, Config())
        assert merge.ready and merge.method == "squash"
        await publish.apply_merge(Git(rb.path), hosting, Config(), merge)
    finally:
        await hosting.aclose()
    assert json.loads(merged.calls.last.request.content) == {"Do": "squash", "head_commit_id": sha}


@respx.mock
async def test_gitlab_create_and_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RD_TEST_TOKEN", TOKEN)
    rb, _ = fix_repo(tmp_path)
    rb.git("remote", "set-url", "origin", "git@gitlab.com:skupina/proj.git")
    sha = rb.git("rev-parse", BRANCH).strip()
    rb.git("update-ref", f"refs/remotes/origin/{BRANCH}", sha)
    api = "https://gitlab.com/api/v4"
    project = f"{api}/projects/skupina%2Fproj"
    config = Config(forges=[ForgeConfig(name="gl", type="gitlab", token_env="RD_TEST_TOKEN")])
    respx.get(project).respond(
        json={"id": 42, "path_with_namespace": "skupina/proj", "default_branch": "main"}
    )
    mr = {
        "iid": 5,
        "web_url": "https://gitlab.com/skupina/proj/-/merge_requests/5",
        "state": "opened",
        "title": SUBJECT,
        "source_branch": BRANCH,
        "target_branch": "main",
        "sha": sha,
        "detailed_merge_status": "mergeable",
    }
    respx.get(f"{api}/projects/42/merge_requests", params={"source_branch": BRANCH}).respond(
        json=[]
    )
    created = respx.post(f"{api}/projects/42/merge_requests").respond(201, json=mr)
    hosting = await publish.connect(Git(rb.path), config, "origin")
    try:
        plan = await publish.plan_pr(Git(rb.path), hosting, Config())
        await publish.apply_pr(Git(rb.path), hosting, Config(), plan)
        body = json.loads(created.calls.last.request.content)
        assert body["source_branch"] == BRANCH and body["remove_source_branch"] is False
        assert created.calls.last.request.headers["private-token"] == TOKEN
        respx.get(f"{api}/projects/42/merge_requests", params={"source_branch": BRANCH}).respond(
            json=[mr]
        )
        respx.get(f"{api}/projects/42/merge_requests/5").respond(json=mr)
        respx.get(f"{api}/projects/42/pipelines").respond(json=[{"id": 9, "status": "success"}])
        respx.get(f"{api}/projects/42/pipelines/9/jobs").respond(
            json=[
                {"name": "test", "status": "success", "web_url": "https://gitlab.com/j/1"},
                {"name": "lint", "status": "failed", "web_url": "https://gitlab.com/j/2"},
                {"name": "deploy", "status": "running", "web_url": ""},
            ]
        )
        merged = respx.put(f"{api}/projects/42/merge_requests/5/merge").respond(200, json=mr)
        merge = await publish.plan_merge(
            Git(rb.path), hosting, Config(publish=PublishConfig(merge_method="squash"))
        )
        assert merge.ready
        assert [(c.name, c.state) for c in merge.ci.checks] == [
            ("test", "success"),
            ("lint", "failure"),
            ("deploy", "pending"),
        ]
        await publish.apply_merge(
            Git(rb.path), hosting, Config(publish=PublishConfig(merge_method="squash")), merge
        )
    finally:
        await hosting.aclose()
    assert json.loads(merged.calls.last.request.content) == {"sha": sha, "squash": True}


# ----------------------------------------------------------------------------- CLI
def test_cli_push_dry_run_then_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from repo_doctor import cli

    rb, bare = fix_repo(tmp_path)
    assert cli.main(["publish", "push", str(rb.path)]) == 0
    out = capsys.readouterr().out
    assert BRANCH in out and SUBJECT in out and "Nic se neodeslalo" in out
    assert remote_branches(bare) == {"main"}
    assert cli.main(["publish", "push", str(rb.path), "--apply"]) == 0
    assert "Odesláno" in capsys.readouterr().out
    assert remote_branches(bare) == {"main", BRANCH}
    assert cli.main(["publish", "push", str(rb.path), "--apply"]) == 1  # už není co poslat
    assert "Není co poslat" in capsys.readouterr().err


def test_cli_merge_local_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from repo_doctor import cli

    rb, _ = fix_repo(tmp_path)
    assert cli.main(["publish", "merge-local", str(rb.path)]) == 0
    assert "fast-forward" in capsys.readouterr().out
    assert rb.git("rev-parse", "main").strip() != rb.git("rev-parse", BRANCH).strip()
    assert cli.main(["publish", "merge-local", str(rb.path), "--apply"]) == 0
    assert rb.git("rev-parse", "main").strip() == rb.git("rev-parse", BRANCH).strip()
    assert "Nic se nepushnulo" in capsys.readouterr().out
    assert cli.main(["publish", "merge-local", str(rb.path), "--apply"]) == 1
    assert cli.main(["publish", "push", str(tmp_path / "neni")]) == 2


def test_cli_pr_without_matching_forge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from repo_doctor import cli

    rb, _ = fix_repo(tmp_path)
    assert cli.main(["publish", "pr", str(rb.path)]) == 1
    assert "nepatří žádnému nastavenému hostingu" in capsys.readouterr().err


# ----------------------------------------------------------------------------- stav (stránka Odeslání)
def test_local_status_follows_the_fix_branch(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    st = publish.local_status(Git(rb.path), Config())
    assert st.branch == BRANCH and st.default == "main" and st.remote == "origin"
    assert [c.subject for c in st.commits] == [SUBJECT]
    assert st.pushed is False and st.can_merge_local and st.default_ahead == 0
    assert st.pr is None and not st.can_merge
    publish.apply_push(Git(rb.path), publish.plan_push(Git(rb.path), Config()), Config())
    assert publish.local_status(Git(rb.path), Config()).pushed is True


def test_local_status_without_fix_branch_or_remote(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path)
    st = publish.local_status(Git(rb.path), Config())
    assert st.branch is None and "nemá žádnou opravnou větev" in st.local_note
    rb.git("remote", "remove", "origin")
    st = publish.local_status(Git(rb.path), Config())
    assert st.remote is None and "žádný remote" in st.hosting_note


def test_local_status_counts_unpushed_default_branch(tmp_path: Path) -> None:
    rb, _ = fix_repo(tmp_path)
    rb.git("merge", "--ff-only", "-q", BRANCH)
    st = publish.local_status(Git(rb.path), Config())
    assert st.default_ahead == 1 and not st.can_merge_local  # fix už je ve výchozí větvi


@respx.mock
async def test_publish_status_shows_pr_and_every_ci_check(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    mock_pr_state(
        sha,
        checks=[
            {"name": "test", "status": "completed", "conclusion": "success", "html_url": "u1"},
            {"name": "lint", "status": "in_progress", "conclusion": None, "html_url": "u2"},
        ],
    )
    st = await publish.publish_status(Git(rb.path), github_config())
    assert st.pr is not None and st.pr.number == 7 and st.pushed
    assert st.ci is not None and st.ci.state == "pending"
    assert [(c.name, c.state, c.url) for c in st.ci.checks] == [
        ("test", "success", "u1"),
        ("lint", "pending", "u2"),
    ]
    assert not st.can_merge and any("CI ještě běží" in b for b in st.blockers)


@respx.mock
async def test_publish_status_ready_when_ci_green(hosted: tuple[RepoBuilder, str]) -> None:
    rb, sha = hosted
    respx.get(REPO_API).respond(json=repo_json())
    mock_pr_state(sha, checks=GREEN)
    st = await publish.publish_status(Git(rb.path), github_config())
    assert st.can_merge and st.ci is not None and st.ci.state == "success"


@respx.mock
async def test_publish_status_without_pr_or_offline(hosted: tuple[RepoBuilder, str]) -> None:
    rb, _ = hosted
    respx.get(REPO_API).respond(json=repo_json())
    respx.get(f"{REPO_API}/pulls").respond(json=[])
    st = await publish.publish_status(Git(rb.path), github_config())
    assert st.pr is None and st.ci is None and st.branch == BRANCH
    off = await publish.publish_status(Git(rb.path), github_config(), online=False)
    assert off.pr is None and "Offline" in off.hosting_note


@respx.mock
async def test_publish_status_explains_missing_hosting(hosted: tuple[RepoBuilder, str]) -> None:
    rb, _ = hosted
    st = await publish.publish_status(Git(rb.path), Config())
    assert st.branch == BRANCH and "nepatří žádnému nastavenému hostingu" in st.hosting_note
