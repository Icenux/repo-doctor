from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
import respx

from repo_doctor import history
from repo_doctor.config import (
    AllowEntry,
    ChecksConfig,
    Config,
    ForgeConfig,
    LimitsConfig,
    RootConfig,
)
from repo_doctor.forges.urls import SshConfig
from repo_doctor.models import Severity
from repo_doctor.scanner import NetServices, ScanEvent, Scanner, ScanOptions, aggregate_pulse
from tests import factory as fx
from tests.factory import RepoBuilder, healthy_python_repo, with_remote
from tests.helpers import NOW


def make_tree(tmp: Path) -> Path:
    root = tmp / "projekty"
    healthy_python_repo(root / "healthy")
    secret = RepoBuilder.create(root / "leaky")
    secret.write("config/prod.env", f"AWS={fx.fake_aws_key()}\n").commit()
    secret.remove("config/prod.env").write("README.md", "x").commit()
    bare = RepoBuilder.create(root / "group" / "noreadme")
    bare.write("main.py", "print(1)\n").commit()
    return root


async def test_scan_offline_end_to_end(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    events: list[ScanEvent] = []
    cfg = Config(roots=[RootConfig(path=str(root))])
    scanner = Scanner(
        cfg,
        ScanOptions(offline=True, jobs=2, now=NOW),
        on_event=events.append,
        ssh_config=SshConfig(),
    )
    result = await scanner.run()
    names = {r.name: r for r in result.repos}
    assert set(names) == {"healthy", "leaky", "noreadme"}
    assert {f.check_id for f in names["healthy"].findings} == {"no-remote"}
    assert any(f.check_id == "secrets-history" for f in names["leaky"].findings)
    assert (names["leaky"].score or 0) < (names["healthy"].score or 0)
    assert names["noreadme"].skipped["deps-vulnerable"] == "offline režim"
    # detaily repa se sbírají i offline
    healthy = names["healthy"].details
    assert healthy.files and healthy.first_commit is not None and healthy.authors
    assert "forge-ci-failing" in names["noreadme"].skipped
    kinds = [e.kind for e in events]
    assert kinds[0] == "discovered" and kinds[-1] == "finished"
    assert kinds.count("repo_done") == 3
    assert any(e.kind == "check" and e.check == "secrets-tree" for e in events)
    assert result.finished_at == NOW
    assert len(aggregate_pulse(result)) == 30


async def test_state_and_remote(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path, ahead=2)
    rb.git("remote", "set-url", "origin", "https://user:hunter2hunter2@github.com/nekdo/proj.git")
    rb.write("dirty.txt", "x")
    cfg = Config(roots=[RootConfig(path=str(rb.path))])
    res = (
        await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    ).repos[0]
    assert res.state.unpushed == 2
    assert res.state.uncommitted == 1
    assert res.state.branch == "main"
    assert res.remotes[0].url == "https://…@github.com/nekdo/proj.git"
    assert "hunter2hunter2" not in res.model_dump_json()
    assert res.web_url == "https://github.com/nekdo/proj"
    assert res.forge == "GitHub"


async def test_allowlist_and_disabled(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    cfg = Config(roots=[RootConfig(path=str(root))])
    first = await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    leaky = next(r for r in first.repos if r.name == "leaky")
    target = next(f for f in leaky.findings if f.check_id == "secrets-history")
    cfg2 = Config(
        roots=cfg.roots,
        allowlist=[AllowEntry(hash=target.fingerprint("leaky"), reason="testovací klíč")],
        checks=ChecksConfig(disabled=["readme-missing"]),
    )
    second = await Scanner(cfg2, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    leaky2 = next(r for r in second.repos if r.name == "leaky")
    assert leaky2.allowlisted == 1
    assert not any(f.check_id == "secrets-history" for f in leaky2.findings)
    assert not any(f.check_id == "readme-missing" for r in second.repos for f in r.findings)


async def test_only_skip_and_overrides(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("main.py", "x").write(
        ".repo-doctor.toml", '[checks]\ndisabled=["license-missing"]\n'
    ).commit()
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    res = await Scanner(
        cfg,
        ScanOptions(offline=True, only=["license-missing", "readme-missing"]),
        ssh_config=SshConfig(),
    ).run()
    assert {f.check_id for f in res.repos[0].findings} == {"readme-missing"}
    (rb.path / ".repo-doctor.toml").write_text("bogus = [")
    res = await Scanner(
        cfg, ScanOptions(offline=True, only=["readme-missing"]), ssh_config=SshConfig()
    ).run()
    assert "config" in res.repos[0].errors
    with pytest.raises(KeyError):
        Scanner(cfg, ScanOptions(only=["nope"]))


async def test_cancel(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    cfg = Config(roots=[RootConfig(path=str(root))])
    scanner: Scanner

    def on_event(e: ScanEvent) -> None:
        if e.kind == "repo_done":
            scanner.cancel()

    scanner = Scanner(
        cfg, ScanOptions(offline=True, jobs=1), on_event=on_event, ssh_config=SshConfig()
    )
    result = await scanner.run()
    assert result.cancelled
    assert scanner.cancelled
    assert len(result.repos) < 3


async def test_check_errors_are_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("a", "a").commit()
    from repo_doctor.checks.hygiene import ReadmeMissing

    def boom(self: object, repo: object) -> list[object]:
        raise OSError("disk hoří")

    monkeypatch.setattr(ReadmeMissing, "run", boom)
    res = await Scanner(
        Config(roots=[RootConfig(path=str(rb.path))]),
        ScanOptions(offline=True),
        ssh_config=SshConfig(),
    ).run()
    assert "disk hoří" in res.repos[0].errors["readme-missing"]


async def test_warnings_for_missing_root(tmp_path: Path) -> None:
    res = await Scanner(
        Config(roots=[RootConfig(path=str(tmp_path / "nope"))]),
        ScanOptions(offline=True),
        ssh_config=SshConfig(),
    ).run()
    assert res.repos == [] and "neexistuje" in res.warnings[0]


@respx.mock
async def test_network_enrichment_with_forge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN_TEST", "ghp_" + "t" * 36)
    rb, _ = with_remote(tmp_path)
    rb.git("remote", "set-url", "origin", "git@github.com:nekdo/proj.git")
    rb.write("requirements.txt", "requests==2.19.0\n").commit()
    sha = rb.head()
    api = "https://api.github.com"
    respx.get(f"{api}/repos/nekdo/proj").respond(
        json={
            "full_name": "nekdo/proj",
            "default_branch": "main",
            "private": False,
            "html_url": "https://github.com/nekdo/proj",
            "description": "Ukázkový projekt",
            "stargazers_count": 4,
            "forks_count": 1,
            "open_issues_count": 3,
            "topics": ["demo"],
            "created_at": "2025-01-01T00:00:00Z",
        }
    )
    respx.get(f"{api}/repos/nekdo/proj/branches/main").respond(
        json={"commit": {"sha": sha}, "protected": False}
    )
    respx.get(f"{api}/repos/nekdo/proj/actions/runs").respond(
        json={
            "workflow_runs": [
                {"status": "completed", "conclusion": "failure", "run_number": 142, "html_url": "u"}
            ]
        }
    )
    respx.get(f"{api}/repos/nekdo/proj/dependabot/alerts").respond(json=[{"n": 1}])
    respx.get(f"{api}/repos/nekdo/proj/secret-scanning/alerts").respond(404)
    respx.get(f"{api}/repos/nekdo/proj/issues").respond(json=[])
    respx.get(f"{api}/user").respond(json={"id": 7, "login": "nekdo"})  # noreply adresa
    respx.post("https://api.osv.dev/v1/querybatch").respond(
        json={"results": [{"vulns": [{"id": "GHSA-x"}]}]}
    )
    respx.get("https://api.osv.dev/v1/vulns/GHSA-x").respond(
        json={"database_specific": {"severity": "HIGH"}}
    )
    respx.get("https://pypi.org/pypi/requests/json").respond(
        json={"info": {"version": "2.32.0", "classifiers": ["Development Status :: 7 - Inactive"]}}
    )
    respx.get("https://pypi.org/pypi/requests/2.19.0/json").respond(
        json={"info": {"license": "Apache 2.0"}}
    )
    cfg = Config(
        roots=[RootConfig(path=str(rb.path))],
        forges=[ForgeConfig(name="github", type="github", token_env="GH_TOKEN_TEST")],
    )
    net = NetServices.create(cfg)
    try:
        result = await Scanner(cfg, ScanOptions(now=NOW), net=net, ssh_config=SshConfig()).run()
    finally:
        await net.aclose()
    res = result.repos[0]
    ids = {f.check_id: f for f in res.findings}
    assert res.visibility == "public"
    assert ids["forge-ci-failing"].message.endswith("(#142)")
    assert "forge-no-branch-protection" in ids
    assert ids["forge-security-alerts"].data["count"] == 1
    assert ids["deps-vulnerable"].severity is Severity.HIGH
    assert "deps-outdated" in ids
    assert "Inactive" in ids["deps-unmaintained"].message  # data z registru bez dalšího dotazu
    assert res.forge_info["ci"] == "failure"
    assert "forge-mirror-drift" not in ids
    d = res.details
    assert d.forge_description == "Ukázkový projekt" and d.forge_topics == ["demo"]
    assert (d.forge_stars, d.forge_forks, d.forge_open_issues) == (4, 1, 3)
    assert d.ecosystems[0].name == "python" and d.ecosystems[0].managers == ["pip"]


@respx.mock
async def test_forge_unavailable_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb, _ = with_remote(tmp_path)
    rb.git("remote", "set-url", "origin", "https://git.home.ts.net:3000/o/proj.git")
    respx.route(host="git.home.ts.net").mock(side_effect=httpx.ConnectError("no route"))
    respx.post("https://api.osv.dev/v1/querybatch").respond(json={"results": []})
    cfg = Config(
        roots=[RootConfig(path=str(rb.path))],
        forges=[
            ForgeConfig(name="home", type="forgejo", url="https://git.home.ts.net:3000"),
            ForgeConfig(name="broken", type="gitlab", token_env="MISSING_VAR_X"),
        ],
    )
    result = await Scanner(
        cfg, ScanOptions(now=NOW), net=NetServices.create(cfg), ssh_config=SshConfig()
    ).run()
    res = result.repos[0]
    assert "nedostupný" in res.skipped["forge-ci-failing"]
    assert any("broken" in w for w in result.warnings)


async def test_no_forges_option(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path)
    cfg = Config(roots=[RootConfig(path=str(rb.path))])
    net = NetServices.create(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    result = await Scanner(
        cfg,
        ScanOptions(use_forges=False, only=["forge-ci-failing"]),
        net=net,
        ssh_config=SshConfig(),
    ).run()
    await net.aclose()
    assert result.repos[0].skipped["forge-ci-failing"] == "hostingy vypnuté (--no-forges)"


def test_history(tmp_path: Path) -> None:
    from tests.sample import sample_result

    r1 = sample_result()
    e1 = history.record(r1, keep=2, directory=tmp_path)
    r2 = sample_result()
    r2.finished_at = r1.finished_at.replace(day=29) if r1.finished_at else None
    r2.repos = r2.repos[:3]
    history.record(r2, keep=2, directory=tmp_path)
    r3 = sample_result()
    history.record(r3, keep=2, directory=tmp_path)
    assert len(history.entries(tmp_path)) == 2
    assert history.delta(e1, None) == {}
    d = history.previous_delta(tmp_path)
    assert isinstance(d, dict)
    assert history.previous_delta(tmp_path / "empty") == {}
    f = history.save_last_scan(r1, tmp_path / "c" / "last.json")
    loaded = history.load_last_scan(f)
    assert loaded is not None and len(loaded.repos) == len(r1.repos)
    assert history.load_last_scan(tmp_path / "missing.json") is None
    (tmp_path / "scan-bad.json").write_text("{")
    assert len(history.entries(tmp_path)) == 2


@pytest.mark.benchmark
async def test_benchmark_50_repos(tmp_path: Path) -> None:
    """Syntetická sada 50 repozitářů musí projít offline do 60 s (viz DECISIONS.md)."""
    root = tmp_path / "bench"
    for i in range(50):
        rb = RepoBuilder.create(root / f"g{i % 5}" / f"repo{i:02d}")
        rb.write("README.md", f"# {i}\n").write("main.py", "print(1)\n" * 50)
        if i % 7 == 0:
            rb.write(".env", "A=1\n")
        rb.commit("c1")
        rb.write("main.py", "print(2)\n").commit("c2")
    cfg = Config(roots=[RootConfig(path=str(root))], limits=LimitsConfig())
    start = time.monotonic()
    result = await Scanner(cfg, ScanOptions(offline=True), ssh_config=SshConfig()).run()
    elapsed = time.monotonic() - start
    assert len(result.repos) == 50
    assert elapsed < 60, f"sken 50 repozitářů trval {elapsed:.1f} s"


async def test_concurrency_respects_jobs(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    running = 0
    peak = 0

    def on_event(e: ScanEvent) -> None:
        nonlocal running, peak
        if e.kind == "repo_started":
            running += 1
            peak = max(peak, running)
        elif e.kind == "repo_done":
            running -= 1

    await Scanner(
        Config(roots=[RootConfig(path=str(root))]),
        ScanOptions(offline=True, jobs=1),
        on_event=on_event,
        ssh_config=SshConfig(),
    ).run()
    assert peak == 1
    await asyncio.sleep(0)


async def test_scan_connects_forges_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#134: token z klíčenky / token_cmd se získává při startu skenu – nesmí blokovat event loop (UI)."""
    import threading

    main = threading.get_ident()
    seen: list[int] = []
    original = NetServices.create.__func__  # type: ignore[attr-defined]

    def recording(cls: type[NetServices], /, *args: object, **kwargs: object) -> NetServices:
        seen.append(threading.get_ident())
        return original(cls, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(NetServices, "create", classmethod(recording))
    cfg = Config(roots=[RootConfig(path=str(make_tree(tmp_path)))])
    await Scanner(cfg, ScanOptions(now=NOW), ssh_config=SshConfig()).run()
    assert seen and main not in seen
