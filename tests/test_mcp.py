"""MCP server (`repo-doctor mcp`): jen čtení, offline ve výchozím stavu, maskování."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import respx

from repo_doctor import history, paths
from repo_doctor.config import Config, MCPConfig, RootConfig
from repo_doctor.gitwrap import Git
from repo_doctor.mcpserver import RepoDoctorTools, build_server
from tests import factory as fx

TOOLS = {
    "list_repos",
    "repo_status",
    "repo_findings",
    "search_findings",
    "last_scan_summary",
    "scan",
    "explain_check",
}


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, str, list[fx.RepoBuilder]]:
    root = tmp_path / "projekty"
    healthy = fx.healthy_python_repo(root / "zdrave")
    secret = fx.fake_aws_key(7)
    leaky = fx.RepoBuilder.create(root / "unik")
    leaky.write("config/prod.env", f"AWS={secret}\n").commit("tajemství")
    leaky.remove("config/prod.env").write("README.md", "x").commit("úklid")
    leaky.write("rozdelane.txt", "necommitnuto")
    return root, secret, [healthy, leaky]


def _config(root: Path, *, network: bool = False) -> Config:
    return Config(roots=[RootConfig(path=str(root))], mcp=MCPConfig(network=network))


def _data(result: Any) -> Any:
    assert not result.is_error, result.content
    return result.structured_content


@respx.mock(assert_all_mocked=True)
async def test_all_tools_read_only_offline_and_masked(
    tree: tuple[Path, str, list[fx.RepoBuilder]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp import Client

    root, secret, builders = tree
    before = [b.snapshot() for b in builders]

    def no_fetch(self: Git, timeout: float = 0) -> None:
        raise AssertionError("MCP nesmí fetchovat")

    monkeypatch.setattr(Git, "fetch", no_fetch)
    tools = RepoDoctorTools(_config(root))
    server = build_server(tools)
    outputs: list[str] = []
    async with Client(server) as client:
        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == TOOLS
        for t in listed.tools:
            assert (t.description and "Example" in t.description) or t.name == "last_scan_summary"
            assert t.annotations is not None and t.annotations.read_only_hint is True

        empty = _data(await client.call_tool("last_scan_summary", {}))
        assert empty["available"] is False
        missing = await client.call_tool("list_repos", {})
        assert missing.is_error  # bez skenu srozumitelná chyba

        summary = _data(await client.call_tool("scan", {"timeout_s": 120}))
        assert summary["available"] and summary["repos"] == 2 and summary["offline"] is True

        repos = _data(await client.call_tool("list_repos", {"state": "problems", "limit": 1}))
        assert repos["page"]["total"] >= 1 and len(repos["repos"]) == 1
        assert repos["repos"][0]["name"] == "unik"
        dirty = _data(await client.call_tool("list_repos", {"state": "dirty"}))
        assert [r["name"] for r in dirty["repos"]] == ["unik"]

        status = _data(await client.call_tool("repo_status", {"repo": "unik"}))
        assert status["uncommitted"] >= 1 and status["branch"] == "main"

        findings = _data(
            await client.call_tool("repo_findings", {"repo": "unik", "min_severity": "high"})
        )
        ids = {f["check_id"] for f in findings["findings"]}
        assert "secrets-history" in ids
        found = _data(await client.call_tool("search_findings", {"check_id": "secrets-history"}))
        assert found["findings"] and all(f["repo"] == "unik" for f in found["findings"])
        by_text = _data(await client.call_tool("search_findings", {"text": "AWS"}))
        assert by_text["page"]["total"] >= 1

        one = _data(await client.call_tool("scan", {"repo": "unik", "timeout_s": 60}))
        assert one["repos"] == 2  # sken jednoho repa doplní výsledek, ostatní zůstanou

        doc = _data(await client.call_tool("explain_check", {"check_id": "secrets-history"}))
        assert "## Postup" in doc["text"]
        bad = await client.call_tool("explain_check", {"check_id": "neexistuje"})
        assert bad.is_error

        for name, args in [
            ("list_repos", {}),
            ("repo_status", {"repo": "unik"}),
            ("repo_findings", {"repo": "unik"}),
            ("search_findings", {}),
            ("last_scan_summary", {}),
        ]:
            res = await client.call_tool(name, args)
            outputs.append(json.dumps(res.structured_content, ensure_ascii=False))
            outputs.extend(getattr(c, "text", "") for c in res.content)

    # žádné tajemství ve výstupech, repa beze změny, výsledek uložen jen do cache repo-doctoru
    assert all(secret not in out for out in outputs)
    assert any(secret[:4] in out for out in outputs)  # maskovaná ukázka je vidět
    assert [b.snapshot() for b in builders] == before
    cached = history.load_last_scan()
    assert cached is not None and {r.name for r in cached.repos} == {"zdrave", "unik"}
    assert paths.cache_dir() in paths.last_scan_file().parents


def test_paging_and_limits(tree: tuple[Path, str, list[fx.RepoBuilder]]) -> None:
    import asyncio

    root, _, _ = tree
    tools = RepoDoctorTools(_config(root), load=lambda: None, save=None)
    asyncio.run(tools.scan())
    first = tools.list_repos(limit=1)
    assert first.page.next_offset == 1 and first.page.total == 2
    second = tools.list_repos(offset=first.page.next_offset or 0, limit=1)
    assert second.page.next_offset is None
    assert {first.repos[0].name, second.repos[0].name} == {"zdrave", "unik"}
    assert tools.list_repos(limit=10_000).page.limit == 100
    with pytest.raises(ValueError, match="Unknown repository"):
        tools.repo_status("nic")


def test_network_option_controls_offline(tree: tuple[Path, str, list[fx.RepoBuilder]]) -> None:
    import asyncio

    from repo_doctor import scanner

    root, _, _ = tree
    seen: list[bool] = []
    original = scanner.Scanner.__init__

    def spy(self: scanner.Scanner, config: Config, options: Any = None, **kw: Any) -> None:
        seen.append(options.offline)
        original(self, config, options, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scanner.Scanner, "__init__", spy)
        mp.setattr(scanner.Scanner, "run", lambda self, *a, **k: _empty())
        asyncio.run(RepoDoctorTools(_config(root), load=lambda: None, save=None).scan())
        asyncio.run(
            RepoDoctorTools(_config(root, network=True), load=lambda: None, save=None).scan()
        )
    assert seen == [True, False]


async def _empty() -> Any:
    from repo_doctor.models import ScanResult

    return ScanResult()


@pytest.mark.skipif(sys.platform == "win32", reason="stdio podproces")
async def test_stdio_server_lists_tools(tmp_path: Path) -> None:
    """Skutečný `repo-doctor mcp` přes stdio (jak ho spustí Claude Code)."""
    import os

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "repo_doctor", "mcp"],
        env={**os.environ},
    )
    async with Client(params) as client:
        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == TOOLS
        res = await client.call_tool("last_scan_summary", {})
        assert res.structured_content == {
            "available": False,
            "finished_at": None,
            "offline": None,
            "repos": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "health": None,
            "warnings": [],
        }


def test_watch_picks_up_changes_made_by_the_app(tmp_path: Path) -> None:
    """Server běží dlouho; allowlist a sken z aplikace (TUI) musí vidět bez restartu (#128)."""
    from repo_doctor.config import AllowEntry, ConfigStore
    from repo_doctor.models import ScanResult

    store = ConfigStore()
    store.load()
    store.save()
    tools = RepoDoctorTools(store.config, store=store, watch=True)
    assert tools.result() is None
    assert tools.config.allowlist == []

    history.save_last_scan(ScanResult())
    assert tools.result() is not None

    ConfigStore().add_allow(AllowEntry(hash="0123456789abcdef", reason="test", check="x", repo="r"))
    assert [a.hash for a in tools.config.allowlist] == ["0123456789abcdef"]
