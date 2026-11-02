"""Oprava zranitelných závislostí (#71): opravené verze z OSV a aktualizace lockfilu."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
import respx

from repo_doctor import deps as D
from repo_doctor import depupdate
from repo_doctor.fixes import apply, plan
from repo_doctor.gitwrap import Git
from repo_doctor.httpclient import HttpClient
from repo_doctor.models import Finding, Severity
from tests.factory import RepoBuilder, git
from tests.helpers import ctx, run

UV_LOCK = """version = 1
requires-python = ">=3.12"

[[package]]
name = "app"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "requests"
version = "2.19.0"
source = { registry = "https://pypi.org/simple" }
"""
PYPROJECT = '[project]\nname = "app"\nversion = "0.1.0"\ndependencies = ["requests"]\n'
TODAY = date(2026, 11, 2)


def _osv_vuln(fixed: list[str], name: str = "requests", eco: str = "PyPI") -> dict[str, object]:
    return {
        "database_specific": {"severity": "HIGH"},
        "affected": [
            {
                "package": {"name": name, "ecosystem": eco},
                "ranges": [
                    {
                        "type": "ECOSYSTEM",
                        "events": [{"introduced": "0"}, *({"fixed": f} for f in fixed)],
                    }
                ],
            }
        ],
    }


@respx.mock
async def test_osv_details_reads_fixed_versions() -> None:
    respx.get("https://api.osv.dev/v1/vulns/GHSA-1").respond(json=_osv_vuln(["2.20.0", "2.31.0"]))
    async with HttpClient() as c:
        vuln = await D.osv_details(c, "GHSA-1", ttl=0)
    assert vuln.fixed == {("PyPI", "requests"): ["2.20.0", "2.31.0"]}


def _dep(version: str = "2.19.0") -> D.Dependency:
    return D.Dependency("PyPI", "Requests", version, "uv.lock", direct=True)


def _vuln(*fixed: str) -> D.Vuln:
    return D.Vuln("GHSA-x", Severity.HIGH, fixed={("PyPI", "requests"): list(fixed)})


def test_fixed_in_takes_lowest_fix_above_current_and_highest_over_vulns() -> None:
    assert D.fixed_in(_dep(), [_vuln("2.20.0", "2.31.0")]) == "2.20.0"
    # dvě zranitelnosti: musí platit oprava obou
    assert D.fixed_in(_dep(), [_vuln("2.20.0"), _vuln("2.5.0", "2.20.0")]) == "2.20.0"
    assert D.fixed_in(_dep(), [_vuln("2.20.0"), _vuln("2.31.0")]) == "2.31.0"
    # oprava v nižší verzi než současná se nepočítá
    assert D.fixed_in(_dep("2.25.0"), [_vuln("2.20.0", "2.31.0")]) == "2.31.0"


def test_fixed_in_none_without_fix() -> None:
    assert D.fixed_in(_dep(), [_vuln()]) is None
    assert D.fixed_in(_dep(), [_vuln("2.1.0")]) is None
    assert D.fixed_in(_dep("není-verze"), [_vuln("2.20.0")]) is None
    assert D.fixed_in(_dep(), [D.Vuln("X", Severity.LOW)]) is None


def _repo(tmp_path: Path, lock: str = UV_LOCK) -> RepoBuilder:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("pyproject.toml", PYPROJECT).write("uv.lock", lock).commit("init")
    return rb


def _data(source: str = "uv.lock", fixed: tuple[str, ...] = ("2.20.0",)) -> D.DepsData:
    dep = D.Dependency("PyPI", "requests", "2.19.0", source, direct=True)
    return D.DepsData(dependencies=[dep], vulns={dep: [_vuln(*fixed)]}, network_done=True)


def test_finding_is_fixable_only_with_fix_and_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb = _repo(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    (f,) = run("deps-vulnerable", rb.path, deps=_data())
    assert f.fixable and f.data["fixed_in"] == "2.20.0" and f.data["package"] == "requests"
    (nofix,) = run("deps-vulnerable", rb.path, deps=_data(fixed=()))
    assert not nofix.fixable and nofix.data["fixed_in"] == ""
    (cargo,) = run("deps-vulnerable", rb.path, deps=_data(source="Cargo.lock"))
    assert not cargo.fixable  # Cargo.lock zatím neumíme
    monkeypatch.setattr(shutil, "which", lambda name: None)
    (missing,) = run("deps-vulnerable", rb.path, deps=_data())
    assert not missing.fixable  # uv není nainstalovaný


def test_target_names_cannot_be_options() -> None:
    assert depupdate.valid_target("requests", "2.20.0")
    assert depupdate.valid_target("@scope/pkg", "1.0.0-beta.1")
    assert not depupdate.valid_target("--registry=evil", "1.0.0")
    assert not depupdate.valid_target("pkg", "--foo")
    assert not depupdate.valid_target("pkg", "")


class FakeTool:
    """Místo uv/npm: přepíše verzi v lockfilu v dočasné složce a zapamatuje si příkaz."""

    def __init__(self, new_version: str = "2.20.0", code: int = 0) -> None:
        self.new_version, self.code = new_version, code
        self.calls: list[tuple[list[str], list[str]]] = []

    def __call__(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, sorted(p.name for p in cwd.iterdir())))
        if self.code == 0:
            lock = cwd / "uv.lock"
            lock.write_text(lock.read_text().replace("2.19.0", self.new_version))
        return subprocess.CompletedProcess(argv, self.code, "", "no solution" if self.code else "")


@pytest.fixture
def uv(monkeypatch: pytest.MonkeyPatch) -> FakeTool:
    tool = FakeTool()
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(depupdate, "run_tool", tool)
    return tool


def _fixable(rb: RepoBuilder) -> list[Finding]:
    return run("deps-vulnerable", rb.path, deps=_data())


def test_fix_updates_lockfile_on_new_branch_only(tmp_path: Path, uv: FakeTool) -> None:
    rb = _repo(tmp_path)
    c = ctx(rb.path)
    patches = plan(c, _fixable(rb))
    (patch,) = patches
    (change,) = patch.changes
    assert change.path == "uv.lock" and "2.20.0" in change.content
    assert patch.summary == "requests 2.19.0→2.20.0"
    (argv, files) = uv.calls[0]
    assert argv[1:] == ["lock", "--upgrade-package", "requests>=2.20.0"]
    assert files == ["pyproject.toml", "uv.lock"]  # do dočasné složky nic dalšího nejde
    assert '+version = "2.20.0"' in patch.diff()

    result = apply(Git(rb.path), patches, today=TODAY)
    assert result.branch == "repo-doctor/fixes-2026-11-02"
    assert "2.20.0" in git(rb.path, "show", f"{result.branch}:uv.lock")
    assert (rb.path / "uv.lock").read_text() == UV_LOCK  # pracovní strom zůstal
    assert git(rb.path, "status", "--porcelain").strip() == ""


def test_fix_reports_tool_failure_without_patch(tmp_path: Path, uv: FakeTool) -> None:
    uv.code = 1
    rb = _repo(tmp_path)
    c = ctx(rb.path)
    (finding,) = _fixable(rb)
    from repo_doctor.checks import get_check

    check = get_check("deps-vulnerable")
    assert check is not None
    assert check.fix(c, [finding]) is None
    update = depupdate.update_lock(
        c.git, c.git.head_sha() or "", "uv.lock", [depupdate.Target("requests", "2.19.0", "2.20.0")]
    )
    assert update.content is None and update.error and "no solution" in update.error


def test_fix_keeps_lock_when_constraint_blocks_update(tmp_path: Path, uv: FakeTool) -> None:
    uv.new_version = "2.19.0"  # nástroj nic nezměnil (omezení v manifestu)
    rb = _repo(tmp_path)
    c = ctx(rb.path)
    assert plan(c, _fixable(rb)) == []
    update = depupdate.update_lock(
        c.git, c.git.head_sha() or "", "uv.lock", [depupdate.Target("requests", "2.19.0", "2.20.0")]
    )
    assert update.unresolved == ["requests 2.19.0"] and update.content is None


def test_update_requires_manifest_next_to_lock(tmp_path: Path, uv: FakeTool) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("uv.lock", UV_LOCK).commit("init")
    c = ctx(rb.path)
    update = depupdate.update_lock(
        c.git, c.git.head_sha() or "", "uv.lock", [depupdate.Target("requests", "2.19.0", "2.20.0")]
    )
    assert update.error and "pyproject.toml" in update.error and uv.calls == []


def test_npm_command_and_transitive_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lock(version: str) -> str:
        return json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "app"},
                    "node_modules/minimist": {"version": version},
                },
            }
        )

    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("package.json", '{"dependencies": {"x": "1"}}').write(
        "package-lock.json", lock("1.2.0")
    ).commit("init")
    seen: list[list[str]] = []

    def fake(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        (cwd / "package-lock.json").write_text(lock("1.2.8"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    c = ctx(rb.path)
    update = depupdate.update_lock(
        c.git,
        c.git.head_sha() or "",
        "package-lock.json",
        [depupdate.Target("minimist", "1.2.0", "1.2.6")],
        runner=fake,
    )
    assert update.upgraded == {"minimist": ("1.2.0", "1.2.8")} and update.content
    assert seen[0][1:] == [
        "update",
        "minimist",
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]


def test_run_tool_hardens_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    proc = depupdate.run_tool(["sh", "-c", "echo $UV_NO_BUILD ${GIT_DIR:-nogit}"], tmp_path)
    assert proc.stdout.strip() == "1 nogit"
