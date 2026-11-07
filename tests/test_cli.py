from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import respx

from repo_doctor import cli, paths
from repo_doctor.checks import check_ids
from tests import factory as fx
from tests.factory import RepoBuilder, healthy_python_repo, with_remote


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    healthy_python_repo(root / "ok")
    leak = RepoBuilder.create(root / "leak")
    leak.write("app.py", f'key = "{fx.fake_aws_key()}"\n').commit()
    return root


def test_no_tty_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "scan" in out and "explain" in out
    assert cli.main(["/some/path"]) == 2  # cesty bez TTY → nápověda, žádné TUI


def test_tty_launches_tui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str] | None] = []
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def fake_tui(roots: list[str] | None) -> int:
        calls.append(roots)
        return 0

    monkeypatch.setattr(cli, "run_tui", fake_tui)
    assert cli.main([]) == 0
    assert cli.main([str(tmp_path)]) == 0
    assert cli.main([str(tmp_path / "missing")]) == 2
    assert calls == [None, [str(tmp_path)]]


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert "repo-doctor" in capsys.readouterr().out


@pytest.mark.parametrize("fmt", ["md", "json", "html"])
def test_scan_formats_and_exit_codes(
    tree: Path, fmt: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["scan", str(tree), "--offline", "--report", fmt, "--jobs", "2"])
    out = capsys.readouterr().out
    assert code == 1  # tajemství = HIGH
    assert fx.fake_aws_key() not in out
    assert "AKIA…(20 znaků)" in out
    if fmt == "json":
        data = json.loads(out)
        assert data["schema_version"] == 2 and data["summary"]["repos"] == 2


def test_scan_thresholds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rb = healthy_python_repo(tmp_path / "r")  # jen no-remote (MED)
    assert cli.main(["scan", str(rb.path), "--offline"]) == 0
    assert cli.main(["scan", str(rb.path), "--offline", "--fail-on", "medium"]) == 1
    assert (
        cli.main(["scan", str(rb.path), "--offline", "--fail-on", "low", "--skip", "no-remote"])
        == 0
    )
    assert (
        cli.main(["scan", str(rb.path), "--offline", "--only", "readme-missing,license-missing"])
        == 0
    )
    capsys.readouterr()


def test_scan_output_file(tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "report.html"
    assert cli.main(["scan", str(tree), "--offline", "-r", "html", "-o", str(out)]) == 1
    assert out.read_text().startswith("<!doctype html>")
    assert cli.main(["scan", str(tree), "--offline", "-o", str(tmp_path / "missing" / "x.md")]) == 2


@pytest.mark.parametrize(
    "args",
    [
        ["scan", "--report", "pdf"],
        ["scan", "--fail-on", "critical"],
        ["scan", "--only", "nope"],
        ["scan"],  # bez konfigurace a cest
        ["explain", "nope"],
        ["forges", "test"],
        ["nonsense-subcommand-flag", "--x"],
        ["scan", "--jobs", "0"],
    ],
)
def test_errors_exit_2(args: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    if args[0] == "scan" and len(args) > 1 and args[1] != "--jobs":
        args = [*args[:1], str(tmp_path), *args[1:]]
    assert cli.main(args) == 2


def test_invalid_config_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text('[[forges]]\nname="x"\ntype="github"\ntoken="plaintext"\n')
    assert cli.main(["scan"]) == 2
    assert "prostý text" in capsys.readouterr().err
    assert cli.main(["config", "validate"]) == 2


def test_config_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["config", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(paths.config_file())
    assert cli.main(["config", "validate"]) == 0
    assert "neexistuje" in capsys.readouterr().out
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text('[[roots]]\npath="/tmp"\n[keys]\nscan_all="F5"\n')
    assert cli.main(["config", "validate"]) == 0
    paths.config_file().write_text('[keys]\nscan_all="j"\n')
    assert cli.main(["config", "validate"]) == 2
    assert "kolize" in capsys.readouterr().err


def test_explain_and_checks(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explain", "secrets-history"]) == 0
    out = capsys.readouterr().out
    assert "git filter-repo" in out and "rotace" in out.lower()
    assert cli.main(["checks"]) == 0
    listing = capsys.readouterr().out
    for cid in check_ids():
        assert cid in listing


def test_every_check_is_documented() -> None:
    from repo_doctor.checkdocs import load

    for cid in check_ids():
        doc = load(cid)
        assert doc is not None, cid
        assert doc.why and doc.steps, cid
    assert load("../etc/passwd") is None


def test_cli_never_modifies_repos(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rb, _ = with_remote(tmp_path, ahead=1)
    rb.write("dirty.txt", "x")
    repos = [RepoBuilder(tree / "ok"), RepoBuilder(tree / "leak"), rb]
    mtimes = [(r.path / ".git" / "index").stat().st_mtime_ns for r in repos]
    before = [r.snapshot() for r in repos]
    cli.main(["scan", str(tree), str(rb.path), "--offline", "-r", "json"])
    cli.main(["scan", str(tree), "--offline", "-r", "md"])
    assert [(r.path / ".git" / "index").stat().st_mtime_ns for r in repos] == mtimes
    assert [r.snapshot() for r in repos] == before
    capsys.readouterr()


@respx.mock
def test_forges_test_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "ghp_" + "Z" * 36
    monkeypatch.setenv("GH_TEST_TOKEN", token)
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        '[[forges]]\nname="github"\ntype="github"\ntoken_env="GH_TEST_TOKEN"\n'
        '[[forges]]\nname="nokey"\ntype="gitlab"\ntoken_env="MISSING_ENV_TOKEN"\n'
        '[[forges]]\nname="home"\ntype="forgejo"\nurl="https://git.home.ts.net:3000"\nverify_tls=false\n'
    )
    respx.get("https://api.github.com/user").respond(
        json={"login": "nekdo"}, headers={"X-OAuth-Scopes": "repo"}
    )
    respx.get("https://api.github.com/user/repos").respond(json=[{"full_name": "a/b"}])
    respx.get("https://git.home.ts.net:3000/api/v1/user").respond(401)
    code = cli.main(["forges", "test"])
    out = capsys.readouterr().out
    assert code == 1
    assert "✓ github" in out and "vidí 1" in out and "široká práva" in out
    assert "✗ nokey" in out and "MISSING_ENV_TOKEN" in out
    assert "✗ home" in out and "TLS je vypnuté" in out
    assert token not in out
    assert cli.main(["forges", "test", "github"]) == 0
    capsys.readouterr()
