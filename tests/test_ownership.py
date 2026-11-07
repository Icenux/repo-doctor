"""Repo cizího vlastníka (safe.directory): jeden nález, žádné další volání gitu, bez skóre."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from repo_doctor import cli, gitwrap
from repo_doctor.checks.meta import UnsafeOwnership
from repo_doctor.config import Config, RootConfig
from repo_doctor.forges.urls import SshConfig
from repo_doctor.gitwrap import Git
from repo_doctor.models import RepoResult, ScanResult
from repo_doctor.reports import markdown
from repo_doctor.scanner import Scanner, ScanOptions
from repo_doctor.scoring import Band, band, gauge, score_label
from repo_doctor.tui.triage_model import TriageState, build_groups
from tests.factory import RepoBuilder
from tests.sample import NOW, sample_result


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "projekty"
    rb = RepoBuilder.create(root / "cizi")
    rb.write("main.py", "print(1)\n").commit()
    return root


def _untrusted() -> RepoResult:
    return RepoResult(
        path="/srv/cizi",
        name="cizi",
        root="~/projekty",
        score=None,
        untrusted_owner=True,
        findings=[UnsafeOwnership().for_path("/srv/cizi")],
    )


async def test_scanner_marks_repo_and_runs_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    calls: list[list[str]] = []
    original = gitwrap.run_process

    def spy(args: Any, *a: Any, **kw: Any) -> Any:
        # jen volání nad repem (globální dotaz na core.sshCommand repo neotevírá)
        if isinstance(args, list) and args[:1] == ["git"] and "-C" in args:
            calls.append(args)
        return original(args, *a, **kw)

    monkeypatch.setattr(gitwrap, "run_process", spy)
    cfg = Config(roots=[RootConfig(path=str(root))])
    result = await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    (repo,) = result.repos
    assert repo.untrusted_owner and repo.score is None
    assert [f.check_id for f in repo.findings] == ["unsafe-ownership"]
    assert repo.skipped == {} and repo.errors == {}
    # jediné volání gitu je detekce, žádné další kontroly
    assert len(calls) == 1 and calls[0][-2:] == ["rev-parse", "--git-dir"]
    assert not any("safe.directory" in a for a in calls[0])
    msg = repo.findings[0].message
    assert f"git config --global --add safe.directory {root / 'cizi'}" in msg
    assert band(repo) is Band.UNCHECKED
    assert result.summary().health == 100


def test_safe_directory_is_never_overridden() -> None:
    cmd = " ".join(Git(Path("/tmp"))._cmd(("status",)))
    assert "safe.directory" not in cmd


def test_disabled_check_keeps_repo_unscored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    root = _repo(tmp_path)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    cfg = Config(roots=[RootConfig(path=str(root))])
    cfg.checks.disabled = ["unsafe-ownership"]
    result = asyncio.run(
        Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    )
    (repo,) = result.repos
    assert repo.findings == [] and repo.score is None


@pytest.mark.parametrize(("fail_on", "code"), [("high", 0), ("low", 1)])
def test_cli_exit_code_follows_severity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_on: str,
    code: int,
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    rc = cli.main(["scan", str(root), "--offline", "--report", "json", "--fail-on", fail_on])
    out = capsys.readouterr().out
    assert rc == code
    data = json.loads(out)
    (repo,) = data["repos"]
    assert repo["score"] is None and repo["untrusted_owner"] is True
    assert repo["band"] == "unchecked"
    assert [f["id"] for f in repo["findings"]] == ["unsafe-ownership"]


def test_markdown_and_helpers_show_dash() -> None:
    result = ScanResult(repos=[_untrusted()])
    text = markdown.render(result, now=NOW)
    assert "| – ····· | `cizi` | NELZE ZKONTROLOVAT |" in text
    assert "## cizi – –/100" in text
    assert score_label(None) == "–" and gauge(None) == "·····"


def test_triage_puts_unchecked_group_last() -> None:
    result = sample_result()
    result.repos.insert(0, _untrusted())
    groups = build_groups(result, TriageState())
    assert groups[-1].band is Band.UNCHECKED
    assert [r.name for r in groups[-1].repos] == ["cizi"]
    health = result.summary(now=NOW).health
    assert health == sample_result().summary(now=NOW).health


def test_html_report_shows_dash_for_unscored_repo() -> None:
    from repo_doctor.reports import html

    text = html.render(ScanResult(repos=[_untrusted()]), now=NOW)
    assert '<span class="score muted">– ·····</span>' in text
    assert "NELZE ZKONTROLOVAT" in text


def test_markdown_lists_errors_with_fetch_detail() -> None:
    repo = _untrusted()
    repo.untrusted_owner, repo.score, repo.findings = False, 100, []
    repo.errors = {"fetch": "Server není dostupný.", "fetch_detail": "Connection refused"}
    text = markdown.render(ScanResult(repos=[repo]), now=NOW)
    assert "Chyby: `fetch`: Server není dostupný., `fetch_detail`: Connection refused" in text


async def test_card_header_variants(tmp_path: Path) -> None:
    from repo_doctor.tui.screens.detail import card_header
    from tests.tui_helpers import make_app

    app = make_app(tmp_path)
    async with app.run_test(size=(100, 30)):
        untrusted = card_header(_untrusted(), app, 100).plain
        assert untrusted.startswith("SKÓRE –/100 ·····")
        assert "nelze zkontrolovat" in untrusted and "safe.directory" in untrusted
        assert "větev" not in untrusted  # žádné zavádějící údaje o stavu gitu

        repo = sample_result().repos[0]
        repo.errors = {"fetch": "Server odmítl SSH klíč.", "fetch_detail": "Permission denied"}
        text = card_header(repo, app, 100).plain
        assert "fetch ✗ Server odmítl SSH klíč.  (git: Permission denied)" in text
        repo.errors = {"fetch": "fatal: něco jiného"}
        assert "(git:" not in card_header(repo, app, 100).plain


async def test_ownership_probe_failure_falls_back_to_normal_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repo_doctor.gitwrap import GitTimeout

    root = _repo(tmp_path)

    def timeout(self: Git) -> bool:
        raise GitTimeout(["rev-parse"], 0.1)

    monkeypatch.setattr(Git, "dubious_ownership", timeout)
    cfg = Config(roots=[RootConfig(path=str(root))])
    result = await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    (repo,) = result.repos
    assert not repo.untrusted_owner and repo.score is not None
    assert "unsafe-ownership" not in {f.check_id for f in repo.findings}
