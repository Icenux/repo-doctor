"""MCP: omezení na sledované složky a zápisové nástroje (vypnuté ve výchozím stavu, náhled)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import respx

from repo_doctor.config import Config, ConfigStore, ForgeConfig, MCPConfig, RootConfig
from repo_doctor.mcpserver import RepoDoctorTools, build_server
from tests import factory as fx

pytestmark = pytest.mark.usefixtures("allow_file")

READ_TOOLS = {
    "list_repos",
    "repo_status",
    "repo_findings",
    "search_findings",
    "last_scan_summary",
    "scan",
    "explain_check",
}
WRITE_TOOLS = {
    "fix_branch": "fix_branch",
    "allowlist": "allowlist_add",
    "sync": "sync_ff",
    "publish": "publish_fix",
}


def make_tools(
    root: Path,
    *,
    writes: list[Any] | None = None,
    restrict: bool = True,
    forges: list[ForgeConfig] | None = None,
) -> RepoDoctorTools:
    config = Config(
        roots=[RootConfig(path=str(root))],
        mcp=MCPConfig(restrict_to_roots=restrict, writes=writes or []),
        forges=forges or [],
    )
    return RepoDoctorTools(config, load=lambda: None, save=None)


def scanned(tools: RepoDoctorTools) -> RepoDoctorTools:
    asyncio.run(tools.scan())
    return tools


def env_repo(root: Path, name: str = "pokus") -> fx.RepoBuilder:
    """Repo s committnutým `.env` (oprava `env-committed` existuje) a čistým pracovním stromem."""
    rb = fx.RepoBuilder.create(root / name)
    rb.write("main.py", "print(1)\n").write(".env", "DEBUG=1\n").commit("init")
    return rb


# ----------------------------------------------------------------- rozsah (restrict_to_roots)
def test_scan_outside_roots_is_refused_by_default(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    env_repo(root, "uvnitr")
    outside = env_repo(tmp_path / "jinde", "venku")
    tools = scanned(make_tools(root))
    with pytest.raises(ValueError, match="outside the watched folders"):
        asyncio.run(tools.scan(str(outside.path)))
    assert [r.name for r in tools.list_repos().repos] == ["uvnitr"]


def test_scan_outside_roots_allowed_when_restriction_is_off(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    env_repo(root, "uvnitr")
    outside = env_repo(tmp_path / "jinde", "venku")
    tools = scanned(make_tools(root, restrict=False))
    asyncio.run(tools.scan(str(outside.path)))
    assert {r.name for r in tools.list_repos().repos} == {"uvnitr", "venku"}


def test_cached_repos_outside_roots_are_hidden(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    env_repo(root, "uvnitr")
    outside = env_repo(tmp_path / "jinde", "venku")
    open_tools = scanned(make_tools(root, restrict=False))
    asyncio.run(open_tools.scan(str(outside.path)))
    strict = make_tools(root)
    strict._result = open_tools._result
    assert [r.name for r in strict.list_repos().repos] == ["uvnitr"]
    assert strict.search_findings(text="venku").page.total == 0
    with pytest.raises(ValueError, match="Unknown repository"):
        strict.repo_status("venku")


# ----------------------------------------------------------------- vypnuté zápisy
def test_writes_are_disabled_by_default(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    env_repo(root)
    tools = scanned(make_tools(root))
    for call in (
        lambda: tools.fix_branch("pokus"),
        lambda: tools.allowlist_add("pokus", "0" * 16, "x"),
        lambda: tools.sync_ff("pokus"),
        lambda: asyncio.run(tools.publish_fix("pokus")),
    ):
        with pytest.raises(ValueError, match="disabled"):
            call()


async def test_only_enabled_write_tools_are_registered(tmp_path: Path) -> None:
    from mcp import Client

    root = tmp_path / "projekty"
    env_repo(root)
    for writes in ([], ["sync"], ["fix_branch", "allowlist", "sync", "publish"]):
        server = build_server(make_tools(root, writes=writes))
        async with Client(server) as client:
            listed = (await client.list_tools()).tools
        names = {t.name for t in listed}
        assert names == READ_TOOLS | {WRITE_TOOLS[w] for w in writes}
        for t in listed:
            assert t.annotations is not None
            assert t.annotations.read_only_hint is (t.name in READ_TOOLS)
            assert t.annotations.destructive_hint is False


# ----------------------------------------------------------------- fix_branch
def test_fix_branch_previews_then_creates_only_a_branch(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    rb = env_repo(root)
    tools = scanned(make_tools(root, writes=["fix_branch"]))
    before = rb.snapshot()

    preview = tools.fix_branch("pokus", ["env-committed"])
    assert preview.applied is False and "env-committed" in preview.summary
    assert ".env" in preview.preview
    assert rb.snapshot() == before

    done = tools.fix_branch("pokus", ["env-committed"], confirm=True)
    assert done.applied is True and done.branch and done.branch.startswith("repo-doctor/fixes-")
    assert "Nothing was pushed" in done.summary
    after = rb.snapshot()
    assert after["head"] == before["head"] and after["symbolic"] == before["symbolic"]
    assert done.branch in fx.git(rb.path, "branch", "--list").replace("*", "")


def test_fix_branch_refuses_dirty_repo_and_unknown_check(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    rb = env_repo(root)
    tools = scanned(make_tools(root, writes=["fix_branch"]))
    with pytest.raises(ValueError, match="No automatically fixable"):
        tools.fix_branch("pokus", ["neexistuje"])
    rb.write("rozdelane.txt", "x")
    rb.git("add", "rozdelane.txt")
    with pytest.raises(ValueError, match="necommitnuté změny"):
        tools.fix_branch("pokus", ["env-committed"], confirm=True)
    assert "repo-doctor/fixes-" not in fx.git(rb.path, "branch", "--list")


def test_writes_never_touch_repos_outside_roots(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    env_repo(root, "uvnitr")
    outside = env_repo(tmp_path / "jinde", "venku")
    tools = scanned(make_tools(root, writes=["fix_branch"], restrict=False))
    asyncio.run(tools.scan(str(outside.path)))  # čtení mimo složky je povolené
    before = outside.snapshot()
    with pytest.raises(ValueError, match="outside the watched folders"):
        tools.fix_branch("venku", ["env-committed"], confirm=True)
    assert outside.snapshot() == before


# ----------------------------------------------------------------- allowlist_add
def test_allowlist_add_previews_then_writes_config(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    env_repo(root)
    store = ConfigStore(tmp_path / "cfg" / "config.toml")
    store.path.parent.mkdir()
    store.load()
    tools = make_tools(root, writes=["allowlist"])
    tools._store = store
    scanned(tools)
    finding = next(
        f for f in tools.repo_findings("pokus").findings if f.check_id == "env-committed"
    )
    assert len(finding.fingerprint) == 16

    preview = tools.allowlist_add("pokus", finding.fingerprint, "testovací hodnota")
    assert preview.applied is False and not store.path.exists()

    done = tools.allowlist_add("pokus", finding.fingerprint, "testovací hodnota", confirm=True)
    assert done.applied is True
    text = store.path.read_text("utf-8")
    assert finding.fingerprint in text and "testovací hodnota" in text
    assert [a.hash for a in tools.config.allowlist] == [finding.fingerprint]
    assert all(f.check_id != "env-committed" for f in tools.repo_findings("pokus").findings)


def test_allowlist_add_validates_input(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    env_repo(root)
    tools = scanned(make_tools(root, writes=["allowlist"]))
    with pytest.raises(ValueError, match="Unknown fingerprint"):
        tools.allowlist_add("pokus", "0" * 16, "x", confirm=True)
    fp = tools.repo_findings("pokus").findings[0].fingerprint
    with pytest.raises(ValueError, match="reason is required"):
        tools.allowlist_add("pokus", fp, "   ", confirm=True)


# ----------------------------------------------------------------- sync_ff
def test_sync_ff_previews_then_fast_forwards(tmp_path: Path) -> None:
    from tests.test_sync import _setup

    local, other = _setup(tmp_path)
    root = tmp_path / "projekty"
    tools = scanned(make_tools(root, writes=["sync"]))
    local.git("fetch", "-q", "origin")
    before = local.head()

    preview = tools.sync_ff("obet")
    assert preview.applied is False and local.head() == before
    assert "b.txt" in preview.preview

    done = tools.sync_ff("obet", confirm=True)
    assert done.applied is True and local.head() == other.head()
    with pytest.raises(ValueError):  # už není co dohánět
        tools.sync_ff("obet", confirm=True)


# ----------------------------------------------------------------- publish_fix
def fix_repo_in(root: Path, tmp_path: Path) -> tuple[fx.RepoBuilder, Path]:
    rb, bare = fx.with_remote(tmp_path, "proj")
    root.mkdir(exist_ok=True)
    moved = root / "proj"
    rb.path.rename(moved)
    rb = fx.RepoBuilder(moved)
    rb.git("checkout", "-q", "-b", "repo-doctor/fixes-2026-10-27")
    rb.write(".gitignore", ".env\n").commit("chore(repo-doctor): gitignore-env\n\nPřidá .env.")
    rb.git("checkout", "-q", "main")
    return rb, bare


def test_publish_fix_previews_then_pushes_and_reports_missing_pr(tmp_path: Path) -> None:
    root = tmp_path / "projekty"
    rb, bare = fix_repo_in(root, tmp_path)
    tools = scanned(make_tools(root, writes=["publish"]))
    branch = "repo-doctor/fixes-2026-10-27"

    preview = asyncio.run(tools.publish_fix("proj"))
    assert preview.applied is False and branch in preview.preview
    assert branch not in fx.git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads")

    done = asyncio.run(tools.publish_fix("proj", confirm=True))
    # lokální remote není hosting → větev se pošle, PR se nevytvoří a je to řečeno otevřeně
    assert done.applied is True and done.pull_request is None
    assert "PR was not created" in done.summary
    assert branch in fx.git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert rb.head()  # HEAD i main beze změny


@respx.mock
def test_publish_fix_creates_pr_on_hosting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RD_TEST_TOKEN", "tok-test-123")
    root = tmp_path / "projekty"
    rb, bare = fix_repo_in(root, tmp_path)
    branch = "repo-doctor/fixes-2026-10-27"
    # „GitHub“ = lokální bare remote, ale API se zeptá na github.com (push jde přes pushurl)
    rb.git("remote", "set-url", "origin", "https://github.com/nekdo/proj.git")
    rb.git("remote", "set-url", "--push", "origin", str(bare))
    api = "https://api.github.com/repos/nekdo/proj"
    respx.get(api).respond(
        json={
            "full_name": "nekdo/proj",
            "default_branch": "main",
            "private": True,
            "html_url": "https://github.com/nekdo/proj",
        }
    )
    respx.get(f"{api}/pulls").respond(json=[])
    created = respx.post(f"{api}/pulls").respond(
        201,
        json={
            "number": 7,
            "html_url": "https://github.com/nekdo/proj/pull/7",
            "state": "open",
            "title": "t",
            "head": {"ref": branch, "sha": "a" * 40},
            "base": {"ref": "main"},
        },
    )
    forges = [ForgeConfig(name="github", type="github", token_env="RD_TEST_TOKEN")]
    tools = scanned(make_tools(root, writes=["publish"], forges=forges))
    done = asyncio.run(tools.publish_fix("proj", confirm=True))
    assert done.applied and done.pull_request == "https://github.com/nekdo/proj/pull/7"
    assert "Nothing was merged" in done.summary
    assert created.call_count == 1
    assert "tok-test-123" not in done.model_dump_json()
