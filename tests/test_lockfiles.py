"""Oprava zranitelných závislostí (#71): čistě Pythonový přepis lockfilů, bez dalších nástrojů.

Fixtury v `tests/fixtures/lockfiles` jsou skutečné výstupy `uv lock` a `npm install
--package-lock-only` (verze připnuté přes `--upgrade-package X==Y`) a skutečné odpovědi PyPI/npm.
Přepis musí dát **stejný text** jako skutečný nástroj.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from repo_doctor import deps as D
from repo_doctor import lockfiles
from repo_doctor.checks import get_check
from repo_doctor.fixes import apply, plan
from repo_doctor.gitwrap import Git
from repo_doctor.httpclient import HttpClient
from repo_doctor.lockfiles import Refused, RegistryError, Target, npm, pypi, semver
from repo_doctor.models import Finding, Severity
from tests.factory import RepoBuilder, git
from tests.helpers import ctx, run

FIX = Path(__file__).parent / "fixtures" / "lockfiles"
UV_BEFORE = (FIX / "uv-before.lock").read_text()
UV_AFTER = (FIX / "uv-after.lock").read_text()
NPM_BEFORE = (FIX / "npm-before.json").read_text()
TODAY = date(2026, 11, 2)
UV_BUMPS = [("markupsafe", "2.1.3", "2.1.5"), ("idna", "3.4", "3.7"), ("pyyaml", "5.3.1", "5.4")]


class FixtureRegistry:
    """Odpovědi registrů uložené v tests/fixtures (testy nikdy nevolají síť)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.overrides: dict[tuple[str, str], dict[str, Any]] = {}

    def _load(self, kind: str, name: str, version: str) -> dict[str, Any]:
        self.calls.append((name, version))
        if (name, version) in self.overrides:
            return self.overrides[(name, version)]
        path = FIX / f"{kind}-{name}-{version}.json"
        if not path.exists():
            raise RegistryError(f"{name} {version}: 404")
        data: dict[str, Any] = json.loads(path.read_text())
        return data

    def pypi_version(self, name: str, version: str) -> dict[str, Any]:
        return self._load("pypi", name.lower(), version)

    def npm_version(self, name: str, version: str) -> dict[str, Any]:
        return self._load("npm", name, version)


@pytest.fixture
def registry() -> FixtureRegistry:
    return FixtureRegistry()


# ------------------------------------------------------------------ opravené verze z OSV


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
    assert D.fixed_in(_dep(), [_vuln("2.20.0"), _vuln("2.5.0", "2.20.0")]) == "2.20.0"
    assert D.fixed_in(_dep(), [_vuln("2.20.0"), _vuln("2.31.0")]) == "2.31.0"
    assert D.fixed_in(_dep("2.25.0"), [_vuln("2.20.0", "2.31.0")]) == "2.31.0"


def test_fixed_in_none_without_fix() -> None:
    assert D.fixed_in(_dep(), [_vuln()]) is None
    assert D.fixed_in(_dep(), [_vuln("2.1.0")]) is None
    assert D.fixed_in(_dep("není-verze"), [_vuln("2.20.0")]) is None
    assert D.fixed_in(_dep(), [D.Vuln("X", Severity.LOW)]) is None


# ------------------------------------------------------------------ semver


@pytest.mark.parametrize(
    ("version", "spec", "expected"),
    [
        ("1.2.6", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("0.2.5", "^0.2.3", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.4", "^0.0.3", False),
        ("1.2.9", "~1.2.3", True),
        ("1.3.0", "~1.2.3", False),
        ("1.9.0", "1.x", True),
        ("2.0.0", "1.x", False),
        ("3.0.0", "*", True),
        ("1.2.6", "", True),
        ("1.2.6", ">=1.2.0 <1.3.0", True),
        ("1.3.0", ">=1.2.0 <1.3.0", False),
        ("1.5.0", "1.0.0 - 1.6", True),
        ("1.7.0", "1.0.0 - 1.6", False),
        ("2.1.0", "^1 || ^2", True),
        ("1.2.6", "1.2.6", True),
        ("1.2.7", "1.2.6", False),
        ("1.2.7", "~1", True),
        ("2.0.0", "~1", False),
        ("1.2.6", ">1.2.5", True),
        ("1.2.6", ">1.2.6", False),
        ("1.2.6", "<=1.2.6", True),
        ("1.2.6", "<1.2.6", False),
    ],
)
def test_semver_satisfies(version: str, spec: str, expected: bool) -> None:
    assert semver.satisfies(version, spec) is expected


@pytest.mark.parametrize(
    "spec", ["latest", "npm:x@1", "git+https://x/y", "^1.2.0-beta.1", "file:../x", "http://a/b.tgz"]
)
def test_semver_refuses_what_it_does_not_understand(spec: str) -> None:
    with pytest.raises(semver.Unsupported):
        semver.satisfies("1.2.6", spec)


# ------------------------------------------------------------------ uv.lock


def test_uv_rewrite_is_identical_to_real_uv(registry: FixtureRegistry) -> None:
    text = UV_BEFORE
    for name, old, new in UV_BUMPS:
        text, version = pypi.update(text, Target(name, old, new), registry)
        assert version == new
    assert text == UV_AFTER


def test_uv_refuses_when_format_cannot_be_reproduced(registry: FixtureRegistry) -> None:
    broken = UV_BEFORE.replace('upload-time = "2023-06-02T21:43:45.578Z"', 'upload-time = "x"')
    with pytest.raises(Refused, match="formát nelze ověřit"):
        pypi.update(broken, Target("markupsafe", "2.1.3", "2.1.5"), registry)
    other_python = UV_BEFORE.replace('requires-python = ">=3.12"', 'requires-python = ">=3.9"')
    with pytest.raises(Refused, match="formát nelze ověřit"):
        pypi.update(other_python, Target("markupsafe", "2.1.3", "2.1.5"), registry)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda t: t.replace("version = 1\n", "version = 2\n", 1), "version = 1"),
        (lambda t: t + '\n[options]\nexclude-newer = "2026-01-01T00:00:00Z"\n', "exclude-newer"),
        (
            lambda t: t.replace(
                "requires-python",
                "environments = [\"sys_platform == 'linux'\"]\nrequires-python",
                1,
            ),
            "prostředí",
        ),
        (
            lambda t: t.replace(
                'source = { registry = "https://pypi.org/simple" }',
                'source = { registry = "https://evil.example/simple" }',
            ),
            "PyPI",
        ),
        (lambda t: t.replace('version = "2.1.3"', 'version = "2.1.2"'), "není"),
    ],
)
def test_uv_refuses_unsupported_lockfiles(
    edit: Any, message: str, registry: FixtureRegistry
) -> None:
    with pytest.raises(Refused, match=message):
        pypi.update(edit(UV_BEFORE), Target("markupsafe", "2.1.3", "2.1.5"), registry)


def test_uv_refuses_non_upgrades(registry: FixtureRegistry) -> None:
    for fixed in ("2.1.3", "2.1.0", "2.2.0rc1"):
        with pytest.raises(Refused, match="není vyšší"):
            pypi.update(UV_BEFORE, Target("markupsafe", "2.1.3", fixed), registry)


def test_uv_refuses_changed_dependencies(registry: FixtureRegistry) -> None:
    meta = json.loads((FIX / "pypi-idna-3.7.json").read_text())
    meta["info"]["requires_dist"] = ["certifi>=2020"]
    registry.overrides[("idna", "3.7")] = meta
    with pytest.raises(Refused, match="jiné závislosti"):
        pypi.update(UV_BEFORE, Target("idna", "3.4", "3.7"), registry)


def test_uv_refuses_when_another_package_forbids_the_fix(registry: FixtureRegistry) -> None:
    text = UV_BEFORE.replace(
        'requires-dist = [\n    { name = "idna" },',
        'requires-dist = [\n    { name = "idna", specifier = "<3.5" },',
    )
    assert text != UV_BEFORE
    with pytest.raises(Refused, match=r"app vyžaduje idna<3\.5"):
        pypi.update(text, Target("idna", "3.4", "3.7"), registry)


def test_uv_registry_error_is_reported_not_raised(registry: FixtureRegistry) -> None:
    with pytest.raises(RegistryError):
        pypi.update(UV_BEFORE, Target("markupsafe", "2.1.3", "9.9.9"), registry)


# ------------------------------------------------------------------ package-lock.json


def test_npm_rewrite_matches_real_npm(registry: FixtureRegistry) -> None:
    new, version = npm.update(NPM_BEFORE, Target("minimist", "1.2.0", "1.2.6"), registry)
    expected = json.loads((FIX / "npm-after.json").read_text())
    expected["packages"][""]["dependencies"]["minimist"] = "^1.2.0"  # package.json se nemění
    assert json.loads(new) == expected and version == "1.2.6"
    assert new.endswith("}\n")


def _edit_npm(**changes: Any) -> str:
    data = json.loads(NPM_BEFORE)
    entry = data["packages"]["node_modules/minimist"]
    entry.update(changes)
    return json.dumps(data, indent=2) + "\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (NPM_BEFORE.replace("\n", "\r\n"), "formátování"),
        (NPM_BEFORE.replace('"lockfileVersion": 3', '"lockfileVersion": 2'), "lockfileVersion 3"),
        ("{není json", "JSON"),
        (
            _edit_npm(resolved="https://npm.corp.example/minimist/-/minimist-1.2.0.tgz"),
            "registru npmjs.org",
        ),
        (_edit_npm(dependencies={"x": "1"}), "jiné závislosti"),
        (_edit_npm(hasInstallScript=True), "instalační skripty"),
        (_edit_npm(name="jiny"), "alias"),
    ],
)
def test_npm_refuses_what_it_cannot_rewrite_safely(
    text: str, message: str, registry: FixtureRegistry
) -> None:
    with pytest.raises(Refused, match=message):
        npm.update(text, Target("minimist", "1.2.0", "1.2.6"), registry)


def test_npm_refuses_when_dependent_forbids_fix(registry: FixtureRegistry) -> None:
    data = json.loads(NPM_BEFORE)
    data["packages"][""]["dependencies"]["minimist"] = "~1.1.0"
    with pytest.raises(Refused, match=r"package\.json vyžaduje minimist ~1\.1\.0"):
        npm.update(
            json.dumps(data, indent=2) + "\n", Target("minimist", "1.2.0", "1.2.6"), registry
        )
    data["packages"][""]["dependencies"]["minimist"] = "npm:jiny@1"
    with pytest.raises(Refused, match="nelze ověřit omezení"):
        npm.update(
            json.dumps(data, indent=2) + "\n", Target("minimist", "1.2.0", "1.2.6"), registry
        )


def test_npm_updates_nested_copies_and_resolution(registry: FixtureRegistry) -> None:
    data = json.loads(NPM_BEFORE)
    nested = dict(data["packages"]["node_modules/minimist"])
    data["packages"]["node_modules/a"] = {
        "version": "1.0.0",
        "dependencies": {"minimist": "^1.2.0"},
    }
    data["packages"]["node_modules/a/node_modules/minimist"] = nested
    new, _version = npm.update(
        json.dumps(data, indent=2) + "\n", Target("minimist", "1.2.0", "1.2.6"), registry
    )
    packages = json.loads(new)["packages"]
    assert {p: e["version"] for p, e in packages.items() if "minimist" in p} == {
        "node_modules/minimist": "1.2.6",
        "node_modules/a/node_modules/minimist": "1.2.6",
    }


def test_npm_refuses_deprecated_or_unverifiable_release(registry: FixtureRegistry) -> None:
    meta = json.loads((FIX / "npm-minimist-1.2.6.json").read_text())
    registry.overrides[("minimist", "1.2.6")] = {**meta, "deprecated": "nepoužívat"}
    with pytest.raises(Refused, match="zastaralá"):
        npm.update(NPM_BEFORE, Target("minimist", "1.2.0", "1.2.6"), registry)
    registry.overrides[("minimist", "1.2.6")] = {
        **meta,
        "dist": {"tarball": meta["dist"]["tarball"]},
    }
    with pytest.raises(Refused, match="kontrolní součet"):
        npm.update(NPM_BEFORE, Target("minimist", "1.2.0", "1.2.6"), registry)


# ------------------------------------------------------------------ update_lock a kontrola


def _repo(tmp_path: Path, name: str = "uv.lock", text: str = UV_BEFORE) -> RepoBuilder:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write(name, text).commit("init")
    return rb


def test_update_lock_collects_upgrades_and_reasons(
    tmp_path: Path, registry: FixtureRegistry
) -> None:
    rb = _repo(tmp_path)
    c = ctx(rb.path)
    update = lockfiles.update_lock(
        c.git,
        c.git.head_sha() or "",
        "uv.lock",
        [
            Target("markupsafe", "2.1.3", "2.1.5"),
            Target("idna", "3.4", "9.9.9"),
            Target("--evil", "1.0.0", "2.0.0"),
        ],
        registry=registry,
    )
    assert update.upgraded == {"markupsafe": ("2.1.3", "2.1.5")} and update.content
    reasons = dict(update.unresolved)
    assert "registr neodpověděl" in reasons["idna 3.4"] and "neplatný" in reasons["--evil 1.0.0"]
    missing = lockfiles.update_lock(
        c.git, c.git.head_sha() or "", "nic.lock", [], registry=registry
    )
    assert missing.error


def _data(source: str = "uv.lock", fixed: tuple[str, ...] = ("2.1.5",)) -> D.DepsData:
    dep = D.Dependency("PyPI", "markupsafe", "2.1.3", source, direct=True)
    vuln = D.Vuln("GHSA-x", Severity.HIGH, fixed={("PyPI", "markupsafe"): list(fixed)})
    return D.DepsData(dependencies=[dep], vulns={dep: [vuln]}, network_done=True)


def test_finding_is_fixable_for_supported_lockfiles_with_fix(tmp_path: Path) -> None:
    rb = _repo(tmp_path)
    (f,) = run("deps-vulnerable", rb.path, deps=_data())
    assert f.fixable and f.data["fixed_in"] == "2.1.5" and f.data["package"] == "markupsafe"
    (nofix,) = run("deps-vulnerable", rb.path, deps=_data(fixed=()))
    assert not nofix.fixable and nofix.data["fixed_in"] == ""
    (cargo,) = run("deps-vulnerable", rb.path, deps=_data(source="Cargo.lock"))
    assert not cargo.fixable  # Cargo.lock zatím neumíme
    (go,) = run("deps-vulnerable", rb.path, deps=_data(source="go.sum"))
    assert not go.fixable


def test_target_names_cannot_be_options() -> None:
    assert lockfiles.valid_target("requests", "2.20.0")
    assert lockfiles.valid_target("@scope/pkg", "1.0.0-beta.1")
    assert not lockfiles.valid_target("--registry=evil", "1.0.0")
    assert not lockfiles.valid_target("pkg", "--foo")
    assert not lockfiles.valid_target("pkg", "")


@pytest.fixture
def offline_registry(monkeypatch: pytest.MonkeyPatch, registry: FixtureRegistry) -> FixtureRegistry:
    monkeypatch.setattr(lockfiles, "HttpRegistry", lambda: registry)
    return registry


def test_fix_creates_branch_with_rewritten_lockfile_only(
    tmp_path: Path, offline_registry: FixtureRegistry
) -> None:
    rb = _repo(tmp_path)
    c = ctx(rb.path)
    findings = run("deps-vulnerable", rb.path, deps=_data())
    (patch,) = plan(c, findings)
    (change,) = patch.changes
    assert change.path == "uv.lock" and "MarkupSafe-2.1.5" in change.content
    assert patch.summary == "markupsafe 2.1.3→2.1.5"
    assert '+version = "2.1.5"' in patch.diff()
    result = apply(Git(rb.path), [patch], today=TODAY)
    assert result.branch == "repo-doctor/fixes-2026-11-02"
    assert "MarkupSafe-2.1.5" in git(rb.path, "show", f"{result.branch}:uv.lock")
    assert (rb.path / "uv.lock").read_text() == UV_BEFORE  # pracovní strom zůstal
    assert git(rb.path, "status", "--porcelain").strip() == ""


def test_fix_without_changes_yields_no_patch(
    tmp_path: Path, offline_registry: FixtureRegistry
) -> None:
    rb = _repo(tmp_path, text=UV_BEFORE.replace("version = 1\n", "version = 2\n", 1))
    check = get_check("deps-vulnerable")
    assert check is not None
    findings: list[Finding] = run("deps-vulnerable", rb.path, deps=_data())
    assert check.fix(ctx(rb.path), findings) is None


# ------------------------------------------------------------------ síť


@respx.mock
def test_http_registry_reads_pypi_and_npm_versions_and_maps_errors() -> None:
    respx.get("https://pypi.org/pypi/markupsafe/2.1.5/json").respond(json={"urls": []})
    respx.get("https://registry.npmjs.org/@scope%2Fpkg/1.0.0").respond(json={"version": "1.0.0"})
    respx.get("https://pypi.org/pypi/nic/1.0/json").respond(404)
    reg = lockfiles.HttpRegistry(ttl=0)
    assert reg.pypi_version("markupsafe", "2.1.5") == {"urls": []}
    assert reg.npm_version("@scope/pkg", "1.0.0") == {"version": "1.0.0"}
    with pytest.raises(RegistryError):
        reg.pypi_version("nic", "1.0")
