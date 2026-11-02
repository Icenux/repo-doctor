from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from repo_doctor.config import Config, ForgeConfig
from repo_doctor.forges import ForgeManager, build_forge
from repo_doctor.forges.base import CIStatus, ForgeRepo, ForgeSnapshot, PullRequest, parse_time
from repo_doctor.forges.tokens import TokenError, _run_cmd, resolve_token
from repo_doctor.masking import install_log_redaction
from tests.factory import RepoBuilder, with_remote
from tests.helpers import NOW, run

TOKEN = "ghp_" + "S3cr3tT0k3n" * 4


def gh(**kw: Any) -> ForgeConfig:
    return ForgeConfig(name="github", type="github", **kw)


def fj(**kw: Any) -> ForgeConfig:
    return ForgeConfig(name="home", type="forgejo", url="https://git.home.ts.net:3000", **kw)


def gl(**kw: Any) -> ForgeConfig:
    return ForgeConfig(name="gl", type="gitlab", **kw)


# ----------------------------------------------------------------------------- GitHub
@respx.mock
async def test_github_full(caplog: pytest.LogCaptureFixture) -> None:
    install_log_redaction()
    api = "https://api.github.com"
    repo_json = {
        "full_name": "nekdo/x",
        "default_branch": "main",
        "private": True,
        "archived": False,
        "html_url": "https://github.com/nekdo/x",
        "ssh_url": "git@github.com:nekdo/x.git",
        "clone_url": "https://github.com/nekdo/x.git",
        "pushed_at": "2026-09-01T10:00:00Z",
    }
    user_route = respx.get(f"{api}/user").respond(
        json={"login": "nekdo"}, headers={"X-OAuth-Scopes": "repo, read:org"}
    )
    respx.get(f"{api}/user/repos", params={"page": "2"}).respond(
        json=[{**repo_json, "full_name": "nekdo/y"}]
    )
    respx.get(f"{api}/user/repos").respond(
        json=[repo_json], headers={"Link": f'<{api}/user/repos?page=2>; rel="next"'}
    )
    respx.get(f"{api}/repos/nekdo/x").respond(json=repo_json)
    respx.get(f"{api}/repos/nekdo/x/branches/main").respond(
        json={"commit": {"sha": "abc"}, "protected": True}
    )
    respx.get(f"{api}/repos/nekdo/x/actions/runs").respond(
        json={"workflow_runs": [{"status": "in_progress"}]}
    )
    respx.get(f"{api}/repos/nekdo/x/dependabot/alerts").respond(403)
    respx.get(f"{api}/repos/nekdo/x/secret-scanning/alerts").respond(json=[{}, {}])
    respx.get(f"{api}/repos/nekdo/x/issues").respond(
        json=[
            {"number": 1, "title": "t", "updated_at": "2026-01-01T00:00:00Z", "pull_request": {}},
            {"number": 2, "title": "i"},
        ]
    )
    forge = build_forge(gh(), token=TOKEN)
    with caplog.at_level(logging.DEBUG):
        report = await forge.test_connection()
        snap = await forge.snapshot("nekdo/x")
    await forge.client.aclose()
    assert report.ok and report.user == "nekdo" and report.repo_count == 2
    assert any("široká práva" in w for w in report.warnings)
    assert user_route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert (
        snap.repo
        and snap.repo.private
        and snap.repo.pushed_at == datetime(2026, 9, 1, 10, tzinfo=UTC)
    )
    assert snap.default_branch_sha == "abc" and snap.protected is True
    assert snap.ci == CIStatus("pending", "main", "", "")
    assert snap.alerts == {"secret_scanning": 2}
    assert [i.kind for i in snap.open_items] == ["pr", "issue"]
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(forge.config)


@respx.mock
async def test_github_ci_states_and_missing() -> None:
    api = "https://api.github.com"
    forge = build_forge(gh(), token=TOKEN)
    repo = ForgeRepo("o/r", "main", False)
    route = respx.get(f"{api}/repos/o/r/actions/runs")
    for runs, expected in [
        ([], "none"),
        ([{"status": "completed", "conclusion": "success"}], "success"),
        ([{"status": "completed", "conclusion": "timed_out"}], "failure"),
        ([{"status": "completed", "conclusion": "cancelled"}], "none"),
    ]:
        route.respond(json={"workflow_runs": runs})
        forge.client.cache._mem.clear()
        assert (await forge.latest_ci_status(repo, "main", None)).state == expected
    route.respond(404)
    forge.client.cache._mem.clear()
    assert (await forge.latest_ci_status(repo, "main", None)).state == "unknown"
    respx.get(f"{api}/repos/o/gone").respond(404)
    respx.get(f"{api}/repos/o/secret").respond(403)
    snap = await forge.snapshot("o/gone")
    assert snap.repo is None and "nezná" in (snap.missing_reason or "")
    snap = await forge.snapshot("o/secret")
    assert "přístup" in (snap.missing_reason or "")
    respx.get(f"{api}/repos/o/r").respond(json={"full_name": "o/r", "default_branch": "main"})
    respx.get(f"{api}/repos/o/r/branches/main").respond(500)
    respx.get(f"{api}/repos/o/r/dependabot/alerts").respond(404)
    respx.get(f"{api}/repos/o/r/secret-scanning/alerts").respond(404)
    respx.get(f"{api}/repos/o/r/issues").respond(502)
    forge.client.cache._mem.clear()
    snap = await forge.snapshot("o/r")
    assert snap.alerts is None and len(snap.errors) == 2 and snap.ci is not None
    await forge.client.aclose()


@respx.mock
async def test_github_invalid_token_and_rate_limit() -> None:
    api = "https://api.github.com"
    respx.get(f"{api}/user").respond(401, json={"message": "Bad credentials"})
    forge = build_forge(gh(), token=TOKEN)
    report = await forge.test_connection()
    assert not report.ok and "401" in (report.error or "")
    assert TOKEN not in (report.error or "")
    respx.get(f"{api}/user").respond(
        403,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(NOW.timestamp()) + 10**9),
        },
    )
    forge.client.cache._mem.clear()
    report = await forge.test_connection()
    assert not report.ok and "rate-limit" in (report.error or "")
    await forge.client.aclose()


@respx.mock
async def test_github_unauthenticated() -> None:
    api = "https://api.github.com"
    respx.get(f"{api}/users/nekdo").respond(json={"login": "nekdo"})
    respx.get(f"{api}/users/nekdo/repos").respond(json=[])
    respx.get(f"{api}/repos/nekdo/pub").respond(json={"full_name": "nekdo/pub", "private": False})
    respx.get(f"{api}/repos/nekdo/priv").respond(404)
    forge = build_forge(gh(user="nekdo"), token=None)
    report = await forge.test_connection()
    assert report.ok and any("60" in w for w in report.warnings)
    assert "authorization" not in {k.lower() for k in forge.client._client.headers}
    assert await forge.public_visibility("nekdo/pub") == "public"  # type: ignore[attr-defined]
    assert await forge.public_visibility("nekdo/priv") == "unknown"  # type: ignore[attr-defined]
    assert not (await build_forge(gh(), token=None).test_connection()).ok
    assert await build_forge(gh(), token=None).list_repos() == []
    await forge.client.aclose()


@respx.mock
async def test_github_org_and_enterprise() -> None:
    respx.get("https://ghe.corp/api/v3/orgs/firma/repos").respond(json=[{"full_name": "firma/a"}])
    forge = build_forge(
        ForgeConfig(name="ghe", type="github", url="https://ghe.corp", org="firma"), token=TOKEN
    )
    assert [r.full_name for r in await forge.list_repos()] == ["firma/a"]
    await forge.client.aclose()


# ----------------------------------------------------------------------------- Gitea / Forgejo
@respx.mock
async def test_forgejo_full() -> None:
    api = "https://git.home.ts.net:3000/api/v1"
    repo = {
        "full_name": "o/r",
        "default_branch": "main",
        "private": False,
        "archived": True,
        "html_url": "https://git.home.ts.net:3000/o/r",
        "ssh_url": "ssh://git@git.home.ts.net:2222/o/r.git",
        "updated_at": "2026-01-01T00:00:00+01:00",
    }
    tok_route = respx.get(f"{api}/user").respond(json={"login": "ja"})
    respx.get(f"{api}/user/repos", params={"limit": "50", "page": "2"}).respond(json=[])
    respx.get(f"{api}/user/repos").respond(
        json=[repo], headers={"Link": f'<{api}/user/repos?limit=50&page=2>; rel="next"'}
    )
    respx.get(f"{api}/version").respond(json={"version": "9.0.0+gitea-1.22.0 forgejo"})
    respx.get(f"{api}/repos/o/r").respond(json=repo)
    respx.get(f"{api}/repos/o/r/branches/main").respond(
        json={"commit": {"id": "def"}, "protected": False}
    )
    respx.get(f"{api}/repos/o/r/commits/def/status").respond(
        json={
            "state": "failure",
            "total_count": 1,
            "statuses": [{"target_url": "https://x/o/r/actions/runs/142"}],
        }
    )
    respx.get(f"{api}/repos/o/r/issues").respond(
        json=[
            {"number": 5, "title": "PR", "pull_request": {}, "updated_at": "2025-01-01T00:00:00Z"}
        ]
    )
    gitea_token = "gitea-" + "token-123456"  # skládáno za běhu, aby to skener nehlásil
    forge = build_forge(fj(token_source="none", verify_tls=False), token=gitea_token)
    report = await forge.test_connection()
    assert report.ok and report.repo_count == 1
    assert any("verify_tls" in w for w in report.warnings)
    assert tok_route.calls[0].request.headers["authorization"] == f"token {gitea_token}"
    snap = await forge.snapshot("o/r")
    assert snap.repo and snap.repo.archived and snap.repo.visibility == "public"
    assert snap.ci == CIStatus("failure", "main", "142", "https://x/o/r/actions/runs/142")
    assert snap.alerts is None and snap.protected is False
    await forge.client.aclose()


@respx.mock
async def test_gitea_variants() -> None:
    api = "https://gitea.example/api/v1"
    cfg = ForgeConfig(name="g", type="gitea", url="https://gitea.example", user="ja")
    respx.get(f"{api}/users/ja").respond(json={"username": "ja"})
    respx.get(f"{api}/users/ja/repos").respond(json=[])
    respx.get(f"{api}/version").respond(json={"version": "11.0.0+forgejo"})
    forge = build_forge(cfg, token=None)
    report = await forge.test_connection()
    assert report.ok and any("forgejo" in w for w in report.warnings)
    repo = ForgeRepo("o/r", "main", False)
    assert (await forge.latest_ci_status(repo, "main", None)).state == "unknown"
    respx.get(f"{api}/repos/o/r/commits/s/status").respond(json={"total_count": 0, "statuses": []})
    assert (await forge.latest_ci_status(repo, "main", "s")).state == "none"
    respx.get(f"{api}/repos/o/r/commits/t/status").respond(404)
    assert (await forge.latest_ci_status(repo, "main", "t")).state == "unknown"
    await forge.client.aclose()
    org = build_forge(
        ForgeConfig(name="g", type="gitea", url="https://gitea.example", org="team"),
        token="tok-123456",
    )
    respx.get(f"{api}/orgs/team/repos").respond(json=[{"full_name": "team/a"}])
    assert [r.full_name for r in await org.list_repos()] == ["team/a"]
    assert (
        await build_forge(
            ForgeConfig(name="g", type="gitea", url="https://gitea.example"), token=None
        ).list_repos()
        == []
    )
    assert not (
        await build_forge(
            ForgeConfig(name="g", type="gitea", url="https://gitea.example"), token=None
        ).test_connection()
    ).ok


@respx.mock
async def test_forgejo_unreachable() -> None:
    respx.route(host="git.home.ts.net").mock(side_effect=httpx.ConnectError("no route to host"))
    forge = build_forge(fj(), token="tok-123456")
    report = await forge.test_connection()
    assert not report.ok and "nedostupné" in (report.error or "")
    await forge.client.aclose()


# ----------------------------------------------------------------------------- GitLab
@respx.mock
async def test_gitlab_full() -> None:
    api = "https://gitlab.com/api/v4"
    proj = {
        "id": 7,
        "path_with_namespace": "g/sub/p",
        "default_branch": "main",
        "visibility": "internal",
        "archived": False,
        "web_url": "https://gitlab.com/g/sub/p",
        "last_activity_at": "2026-09-01T00:00:00Z",
    }
    respx.get(f"{api}/user").respond(json={"username": "ja"})
    respx.get(f"{api}/personal_access_tokens/self").respond(json={"scopes": ["api", "read_api"]})
    respx.get(f"{api}/projects").respond(json=[proj])
    respx.get(f"{api}/projects/g%2Fsub%2Fp").respond(json=proj)
    respx.get(f"{api}/projects/7/repository/branches/main").respond(
        json={"commit": {"id": "aaa"}, "protected": True}
    )
    pipelines = respx.get(f"{api}/projects/7/pipelines")
    pipelines.respond(json=[{"status": "failed", "iid": 12, "web_url": "u"}])
    respx.get(f"{api}/projects/7/merge_requests").respond(
        json=[{"iid": 1, "title": "mr", "updated_at": "2026-01-01T00:00:00Z"}]
    )
    respx.get(f"{api}/projects/7/issues").respond(json=[{"iid": 2, "title": "is"}])
    forge = build_forge(gl(), token="glpat-" + "x" * 20)
    report = await forge.test_connection()
    assert report.ok and report.scopes == ["api", "read_api"]
    assert any("read_api" in w for w in report.warnings)
    snap = await forge.snapshot("g/sub/p")
    assert snap.repo and snap.repo.private and snap.repo.full_name == "g/sub/p"
    assert snap.ci == CIStatus("failure", "main", "12", "u")
    assert [(i.number, i.kind) for i in snap.open_items] == [(1, "pr"), (2, "issue")]
    for status, expected in [("success", "success"), ("running", "pending"), ("canceled", "none")]:
        pipelines.respond(json=[{"status": status}])
        forge.client.cache._mem.clear()
        assert (await forge.latest_ci_status(snap.repo, "main", None)).state == expected
    pipelines.respond(json=[])
    forge.client.cache._mem.clear()
    assert (await forge.latest_ci_status(snap.repo, "main", None)).state == "none"
    pipelines.respond(403)
    forge.client.cache._mem.clear()
    assert (await forge.latest_ci_status(snap.repo, "main", None)).state == "unknown"
    await forge.client.aclose()


@respx.mock
async def test_gitlab_variants() -> None:
    api = "https://gl.self.example/api/v4"
    respx.get(f"{api}/users", params={"username": "ja"}).respond(json=[{"username": "ja"}])
    respx.get(f"{api}/users/ja/projects").respond(json=[])
    respx.get(f"{api}/groups/team%2Fsub/projects").respond(
        json=[{"path_with_namespace": "team/sub/a"}]
    )
    anon = build_forge(
        ForgeConfig(name="g", type="gitlab", url="https://gl.self.example", user="ja"), token=None
    )
    assert (await anon.test_connection()).ok
    grp = build_forge(
        ForgeConfig(name="g", type="gitlab", url="https://gl.self.example", org="team/sub"),
        token="t" * 20,
    )
    assert [r.full_name for r in await grp.list_repos()] == ["team/sub/a"]
    respx.get(f"{api}/user").respond(json={"username": "x"})
    respx.get(f"{api}/personal_access_tokens/self").respond(404)
    respx.get(f"{api}/projects").respond(json=[])
    tok = build_forge(
        ForgeConfig(name="g", type="gitlab", url="https://gl.self.example"), token="t" * 20
    )
    rep = await tok.test_connection()
    assert rep.ok and any("nejde ověřit" in w for w in rep.warnings)
    none = build_forge(
        ForgeConfig(name="g", type="gitlab", url="https://gl.self.example"), token=None
    )
    assert not (await none.test_connection()).ok and await none.list_repos() == []
    for f in (anon, grp, tok, none):
        await f.client.aclose()


# ----------------------------------------------------------------------------- tokeny
def test_token_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOK", " abcdef123456 \n")
    assert resolve_token(gh(token_env="MY_TOK")).reveal() == "abcdef123456"  # type: ignore[union-attr]
    monkeypatch.delenv("MY_TOK")
    with pytest.raises(TokenError, match="MY_TOK"):
        resolve_token(gh(token_env="MY_TOK"))
    assert resolve_token(gh()) is None
    tok = resolve_token(gh(token_source="keyring"), keyring_get=lambda n: "kr-token-123456")
    assert tok is not None and tok.reveal() == "kr-token-123456"
    with pytest.raises(TokenError, match="klíčence"):
        resolve_token(gh(token_source="keyring"), keyring_get=lambda n: None)
    tok = resolve_token(gh(token_cmd="pass show x"), cmd_runner=lambda c, t: "cmd-token-123456")
    assert tok is not None and tok.reveal() == "cmd-token-123456"


def test_token_cmd_runner_never_leaks(tmp_path: Path) -> None:
    py = sys.executable
    secret = "super-" + "secret-output-9876"
    assert _run_cmd(f"{py} -c \"print(''); print('{secret}'); print('second')\"", 5) == secret
    with pytest.raises(TokenError) as exc:
        _run_cmd(f"{py} -c \"import sys; print('{secret}'); sys.exit(3)\"", 5)
    assert secret not in str(exc.value) and "3" in str(exc.value)
    with pytest.raises(TokenError, match="nalezen"):
        _run_cmd("/nonexistent/prog", 5)
    with pytest.raises(TokenError, match="žádný výstup"):
        _run_cmd(f"{py} -c pass", 5)
    with pytest.raises(TokenError, match="nedoběhl"):
        _run_cmd(f'{py} -c "import time; time.sleep(3)"', 0.2)
    with pytest.raises(TokenError, match="prázdný"):
        _run_cmd("", 5)
    # potomek na pozadí drží stdout otevřený – timeout musí platit i tak (dřív to viselo)
    import time

    start = time.monotonic()
    with pytest.raises(TokenError, match="nedoběhl"):
        _run_cmd("sh -c 'sleep 30 & echo tok-123456; wait'", 0.5)
    assert time.monotonic() - start < 5
    with pytest.raises(TokenError, match="rozparsovat"):
        _run_cmd('"unclosed', 5)


def test_keyring_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backend import KeyringBackend

    store: dict[tuple[str, str], str] = {}

    class Mem(KeyringBackend):
        priority = 1

        def get_password(self, service: str, username: str) -> str | None:
            return store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            del store[(service, username)]

    keyring.set_keyring(Mem())
    from repo_doctor.forges import tokens

    tokens.store_in_keyring("gh", "stored-token-1234")
    assert tokens._keyring_get("gh") == "stored-token-1234"
    tok = resolve_token(ForgeConfig(name="gh", type="github", token_source="keyring"))
    assert tok is not None and tok.reveal() == "stored-token-1234"
    tokens.delete_from_keyring("gh")
    tokens.delete_from_keyring("gh")  # neexistující – bez chyby
    assert tokens._keyring_get("gh") is None

    class Broken(Mem):
        def get_password(self, service: str, username: str) -> str | None:
            raise RuntimeError("dbus")

        def set_password(self, service: str, username: str, password: str) -> None:
            raise RuntimeError("dbus")

    keyring.set_keyring(Broken())
    with pytest.raises(TokenError, match="není dostupná"):
        tokens._keyring_get("gh")
    with pytest.raises(TokenError, match="nelze uložit"):
        tokens.store_in_keyring("gh", "x" * 10)


def test_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OK_TOKEN", "abcdef123456")
    cfg = Config(
        forges=[
            gh(token_env="OK_TOKEN"),
            ForgeConfig(name="bad", type="gitlab", token_env="NOPE_TOKEN"),
            ForgeConfig(name="off", type="gitlab", enabled=False),
            ForgeConfig(
                name="ca",
                type="gitea",
                url="https://x.example",
                ca_bundle=str(tmp_path / "missing.pem"),
            ),
        ]
    )
    mgr = ForgeManager.from_config(cfg, cache_dir=tmp_path)
    assert set(mgr.forges) == {"github"}
    assert "NOPE_TOKEN" in mgr.unavailable["bad"]
    assert "ca" in mgr.unavailable


def test_parse_time() -> None:
    assert parse_time(None) is None
    assert parse_time("garbage") is None
    assert parse_time("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)


# ----------------------------------------------------------------------------- kontroly hostingu
def snap_for(**kw: Any) -> ForgeSnapshot:
    base: dict[str, Any] = {"forge": "github", "kind": "github", "owner_path": "o/r"}
    base.update(kw)
    return ForgeSnapshot(**base)


def test_forge_checks(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path)
    head = rb.head()
    repo = ForgeRepo("o/r", "main", False)
    from repo_doctor.checks import SkipCheck

    with pytest.raises(SkipCheck, match="nepatří"):
        run("forge-ci-failing", rb.path)
    unknown = snap_for(missing_reason="hosting repo nezná")
    (f,) = run("forge-unknown-remote", rb.path, forge_snapshot=unknown)
    assert "nezná" in f.message
    with pytest.raises(SkipCheck):
        run("forge-ci-failing", rb.path, forge_snapshot=unknown)
    assert run("forge-unknown-remote", rb.path, forge_snapshot=snap_for(repo=repo)) == []
    ok = snap_for(
        repo=repo,
        default_branch_sha=head,
        ci=CIStatus("success", "main"),
        protected=True,
        alerts={},
    )
    for cid in (
        "forge-mirror-drift",
        "forge-ci-failing",
        "forge-no-branch-protection",
        "forge-security-alerts",
        "forge-stale-prs",
        "forge-archived-active",
    ):
        assert run(cid, rb.path, forge_snapshot=ok) == [], cid
    bad = snap_for(
        repo=ForgeRepo("o/r", "master", False, archived=False),
        default_branch_sha="f" * 40,
        ci=CIStatus("failure", "master", "142"),
        protected=False,
        alerts={"dependabot": 3, "secret_scanning": 1, "code_scanning": 0},
        open_items=[
            PullRequest(1, "old pr", NOW - timedelta(days=90)),
            PullRequest(2, "old issue", NOW - timedelta(days=60), "issue"),
            PullRequest(3, "fresh", NOW - timedelta(days=1)),
        ],
    )
    drift = run("forge-mirror-drift", rb.path, forge_snapshot=bad)
    assert any("lokálně main, na hostingu master" in f.message for f in drift)
    (ci,) = run("forge-ci-failing", rb.path, forge_snapshot=bad)
    assert "(#142)" in ci.message
    assert len(run("forge-no-branch-protection", rb.path, forge_snapshot=bad)) == 1
    alerts = {f.key: f for f in run("forge-security-alerts", rb.path, forge_snapshot=bad)}
    assert set(alerts) == {"dependabot", "secret_scanning"}
    (stale,) = run("forge-stale-prs", rb.path, forge_snapshot=bad)
    assert "1 otevřený PR a 1 issue" in stale.message
    with pytest.raises(SkipCheck):
        run("forge-security-alerts", rb.path, forge_snapshot=snap_for(repo=repo))
    with pytest.raises(SkipCheck):
        run("forge-no-branch-protection", rb.path, forge_snapshot=snap_for(repo=repo))
    with pytest.raises(SkipCheck):
        run("forge-ci-failing", rb.path, forge_snapshot=snap_for(repo=repo, ci=CIStatus("unknown")))


def test_mirror_drift_variants(tmp_path: Path) -> None:
    rb, _ = with_remote(tmp_path)
    old = rb.head()
    rb.write("n.txt", "n").commit()
    repo = ForgeRepo("o/r", "main", False)
    # lokálně napřed – to hlásí `unpushed`, ne drift
    assert (
        run(
            "forge-mirror-drift",
            rb.path,
            forge_snapshot=snap_for(repo=repo, default_branch_sha=old),
        )
        == []
    )
    rb.git("reset", "-q", "--hard", old)
    rb.git("checkout", "-q", "-b", "side")
    newer = rb.write("s.txt", "s").commit()
    rb.checkout("main")
    (f,) = run(
        "forge-mirror-drift", rb.path, forge_snapshot=snap_for(repo=repo, default_branch_sha=newer)
    )
    assert "+0 / −1" in f.message


def test_archived_active(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("a", "a").commit(ts=int(NOW.timestamp()))
    repo = ForgeRepo("o/r", "main", False, archived=True, pushed_at=NOW - timedelta(days=10))
    (f,) = run("forge-archived-active", rb.path, forge_snapshot=snap_for(repo=repo))
    assert "archivované" in f.message
    repo2 = ForgeRepo("o/r", "main", False, archived=True, pushed_at=NOW + timedelta(days=1))
    assert run("forge-archived-active", rb.path, forge_snapshot=snap_for(repo=repo2)) == []
    assert (
        run(
            "forge-no-branch-protection",
            rb.path,
            forge_snapshot=snap_for(repo=repo, protected=False),
        )
        == []
    )


async def test_invalid_owner_path_not_queried() -> None:
    from repo_doctor.forges.base import valid_owner_path

    assert (
        valid_owner_path("g/sub/p")
        and not valid_owner_path("a/../../user?x/b")
        and not valid_owner_path("solo")
    )
    forge = build_forge(
        gh(), token=TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    snap = await forge.snapshot("a/../user?x")
    assert snap.repo is None and "neplatná" in (snap.missing_reason or "")
    await forge.client.aclose()


# ----------------------------------------------------------------------------- joby CI (graf průběhu)
def pr_info(sha: str = "abc", branch: str = "fix") -> Any:
    from repo_doctor.forges.base import PullRequestInfo

    return PullRequestInfo(1, "u", "open", "t", branch, "main", sha)


@respx.mock
async def test_github_pr_ci_jobs_with_steps() -> None:
    api = "https://api.github.com/repos/o/r"
    respx.get(f"{api}/commits/abc/check-runs").respond(json={"check_runs": []})
    respx.get(f"{api}/commits/abc/status").respond(json={"state": "pending", "total_count": 0})
    respx.get(f"{api}/actions/runs").respond(
        json={"workflow_runs": [{"id": 5, "name": "CI", "path": ".github/workflows/ci.yml@refs/x"}]}
    )
    respx.get(f"{api}/actions/runs/5/jobs").respond(
        json={
            "jobs": [
                {
                    "name": "test",
                    "status": "in_progress",
                    "conclusion": None,
                    "html_url": "https://github.com/o/r/actions/runs/5/job/9",
                    "started_at": "2026-11-02T10:00:00Z",
                    "steps": [
                        {
                            "name": "Run tests",
                            "number": 2,
                            "status": "in_progress",
                            "conclusion": None,
                        },
                        {
                            "name": "Set up job",
                            "number": 1,
                            "status": "completed",
                            "conclusion": "success",
                            "started_at": "2026-11-02T10:00:00Z",
                            "completed_at": "2026-11-02T10:00:03Z",
                        },
                    ],
                },
                {"name": "lint", "status": "completed", "conclusion": "failure"},
                {"name": "docs", "status": "completed", "conclusion": "skipped"},
                {"name": "later", "status": "queued", "conclusion": None},
            ]
        }
    )
    forge = build_forge(gh(), token="ghp_" + "x" * 20)
    repo = ForgeRepo("o/r", "main", False, html_url="u")
    ci = await forge.pr_ci(repo, pr_info())
    states = {j.name: j.state for j in ci.jobs}
    assert states == {"test": "running", "lint": "failure", "docs": "skipped", "later": "queued"}
    test = ci.jobs[0]
    assert test.workflow == "CI" and test.workflow_path == ".github/workflows/ci.yml"
    assert [(s.name, s.state) for s in test.steps] == [
        ("Set up job", "success"),
        ("Run tests", "running"),
    ]
    assert test.started is not None and test.steps[0].completed is not None
    await forge.client.aclose()


@respx.mock
async def test_github_jobs_unavailable_do_not_break_ci_summary() -> None:
    api = "https://api.github.com/repos/o/r"
    respx.get(f"{api}/commits/abc/check-runs").respond(
        json={"check_runs": [{"name": "x", "status": "completed", "conclusion": "success"}]}
    )
    respx.get(f"{api}/commits/abc/status").respond(json={"state": "pending", "total_count": 0})
    respx.get(f"{api}/actions/runs").respond(403, json={"message": "forbidden"})
    forge = build_forge(gh(), token="ghp_" + "x" * 20)
    ci = await forge.pr_ci(ForgeRepo("o/r", "main", False, html_url="u"), pr_info())
    assert ci.state == "success" and ci.jobs == []
    await forge.client.aclose()


@respx.mock
async def test_gitlab_pr_ci_jobs_by_stage() -> None:
    api = "https://gitlab.com/api/v4"
    respx.get(f"{api}/projects/7/pipelines").respond(
        json=[{"id": 9, "iid": 3, "status": "running"}]
    )
    respx.get(f"{api}/projects/7/pipelines/9/jobs").respond(
        json=[
            {
                "id": 3,
                "name": "e2e",
                "stage": "test",
                "status": "running",
                "started_at": "2026-11-02T10:00:00Z",
            },
            {"id": 1, "name": "lint", "stage": "check", "status": "success"},
            {"id": 2, "name": "unit", "stage": "test", "status": "failed"},
            {"id": 4, "name": "ship", "stage": "deploy", "status": "manual"},
            {"id": 5, "name": "wait", "stage": "deploy", "status": "created"},
        ]
    )
    forge = build_forge(gl(), token="glpat-" + "x" * 20)
    ci = await forge.pr_ci(ForgeRepo("g/p", "main", False, html_url="u", id="7"), pr_info())
    assert [(j.name, j.stage, j.state) for j in ci.jobs] == [
        ("lint", "check", "success"),
        ("unit", "test", "failure"),
        ("e2e", "test", "running"),
        ("ship", "deploy", "skipped"),
        ("wait", "deploy", "queued"),
    ]
    assert ci.jobs[0].workflow == "Pipeline #3"
    await forge.client.aclose()


@respx.mock
async def test_gitea_pr_ci_jobs_are_a_flat_list_per_workflow() -> None:
    api = "https://git.home.ts.net:3000/api/v1"
    respx.get(f"{api}/repos/o/r/commits/abc/status").respond(
        json={
            "state": "failure",
            "total_count": 3,
            "statuses": [
                {"context": "CI / test (push)", "status": "success", "target_url": "u1"},
                {"context": "CI / lint (push)", "status": "failure", "target_url": "u2"},
                {"context": "externí", "status": "pending", "target_url": ""},
            ],
        }
    )
    forge = build_forge(fj(), token="tok-123456")
    ci = await forge.pr_ci(ForgeRepo("o/r", "main", False, html_url="u"), pr_info())
    assert [(j.workflow, j.name, j.state) for j in ci.jobs] == [
        ("CI", "test (push)", "success"),
        ("CI", "lint (push)", "failure"),
        ("", "externí", "running"),
    ]
    await forge.client.aclose()
