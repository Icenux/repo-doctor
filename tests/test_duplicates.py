"""Duplicitní klony: stejný remote (SSH i HTTPS, alias) nebo kořenový commit bez remote."""

from __future__ import annotations

from pathlib import Path

from repo_doctor.checks.duplicates import duplicate_groups, remote_key
from repo_doctor.config import Config, RootConfig
from repo_doctor.forges.urls import SshConfig
from repo_doctor.models import RemoteInfo, RepoResult
from repo_doctor.scanner import Scanner, ScanOptions
from repo_doctor.tui.triage_model import TriageState, build_groups
from tests.factory import RepoBuilder, git
from tests.sample import NOW


def _clone_pair(tmp_path: Path) -> tuple[Path, list[RepoBuilder]]:
    root = tmp_path / "projekty"
    a = RepoBuilder.create(root / "prace" / "proj")
    a.write("a.txt", "a").commit("init")
    a.remote("origin", "git@github.com:Ja/Proj.git")
    b = RepoBuilder.create(root / "stare" / "proj")
    git(b.path, "pull", "-q", str(a.path), "main")
    b.remote("origin", "https://github.com/ja/proj")
    b.write("rozdelane.txt", "x")  # necommitnuté změny
    c = RepoBuilder.create(root / "jine")
    c.write("x", "x").commit()
    c.remote("origin", "https://github.com/ja/jine.git")
    # bez remote, stejný kořen
    d = RepoBuilder.create(root / "lokalni-1")
    d.write("r", "r").commit("root")
    e = RepoBuilder.create(root / "lokalni-2")
    git(e.path, "pull", "-q", str(d.path), "main")
    git(a.path, "worktree", "add", "-q", str(root / "proj-feature"), "-b", "feature")
    return root, [a, b, c, d, e]


async def test_scanner_reports_duplicates(tmp_path: Path) -> None:
    root, builders = _clone_pair(tmp_path)
    before = [b.snapshot() for b in builders]
    cfg = Config(roots=[RootConfig(path=str(root))])
    result = await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    by_name = {r.path.split("projekty/")[-1]: r for r in result.repos}
    dup = {
        k: [f for f in r.findings if f.check_id == "duplicate-clone"] for k, r in by_name.items()
    }
    assert len(dup["prace/proj"]) == 1 and len(dup["stare/proj"]) == 1
    assert dup["jine"] == []
    assert len(dup["lokalni-1"]) == 1 and len(dup["lokalni-2"]) == 1
    # kopie bez remote se ke smazání nenabízí (nepushnuté commity nejde spočítat)
    assert {dup[k][0].data["removable"] for k in ("lokalni-1", "lokalni-2")} == {"no"}
    assert dup["proj-feature"] == []  # git worktree není duplicitní klon
    stare = dup["stare/proj"][0]
    assert "github.com/ja/proj" in stare.message
    assert stare.data["removable"] == "no"  # má necommitnuté změny
    assert "necommitnuté 1" in str(stare.data["compare"])
    # skóre započítává nový nález, repa beze změny
    assert by_name["prace/proj"].score is not None and by_name["prace/proj"].score < 100
    assert [b.snapshot() for b in builders] == before


async def test_single_repo_scan_does_not_report_duplicates(tmp_path: Path) -> None:
    from repo_doctor.discovery import discover

    root, _ = _clone_pair(tmp_path)
    cfg = Config(roots=[RootConfig(path=str(root))])
    repos = discover(cfg.roots).repos
    result = await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run(
        cfg.roots, repos[:1]
    )
    assert all(f.check_id != "duplicate-clone" for r in result.repos for f in r.findings)


async def test_disabled_check(tmp_path: Path) -> None:
    root, _ = _clone_pair(tmp_path)
    cfg = Config(roots=[RootConfig(path=str(root))])
    cfg.checks.disabled = ["duplicate-clone"]
    result = await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    assert all(f.check_id != "duplicate-clone" for r in result.repos for f in r.findings)


def test_remote_key_normalization() -> None:
    def repo(url_host: str, path: str, name: str = "origin") -> RepoResult:
        return RepoResult(
            path="/x",
            name="x",
            root="~",
            remotes=[RemoteInfo(name=name, url="u", host=url_host, owner_path=path)],
        )

    assert remote_key(repo("GitHub.com.", "Ja/Proj.git")) == "github.com/ja/proj"
    assert remote_key(repo("github.com", "ja/proj")) == "github.com/ja/proj"
    assert remote_key(repo("ssh.github.com", "ja/proj")) == "github.com/ja/proj"
    assert remote_key(RepoResult(path="/y", name="y", root="~")) is None


def test_untrusted_and_triage_grouping() -> None:
    from repo_doctor.models import ScanResult

    def r(path: str, host: str | None, untrusted: bool = False) -> RepoResult:
        remotes = [RemoteInfo(name="origin", url="u", host=host, owner_path="ja/p")] if host else []
        return RepoResult(
            path=path, name=path, root="~", remotes=remotes, untrusted_owner=untrusted
        )

    result = ScanResult(repos=[r("/a", "github.com"), r("/b", "github.com", True), r("/c", None)])
    assert duplicate_groups(result) == []  # cizí vlastník se nepočítá
    state = TriageState(group_by="remote")
    groups = build_groups(result, state)
    labels = sorted(g.label for g in groups)
    assert labels == ["bez remote", "github.com/ja/p"]
