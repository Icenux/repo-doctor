"""`http://` jen pro hosty z `allow_http_hosts` (0.2.0, část A)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from repo_doctor import cli, netpolicy, paths
from repo_doctor.config import Config, ConfigError, ForgeConfig, parse_config
from repo_doctor.forges import ForgeManager, build_forge, insecure_reason
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.httpclient import HttpClient, InsecureHostError
from repo_doctor.netpolicy import HttpHostPolicy, http_allowed_for, parse_rule
from repo_doctor.sshhelp import explain_ssh_error
from tests.factory import RepoBuilder, git
from tests.httpgit import GitHttpServer, git_http_server

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="git http-backend")


# ------------------------------------------------------------------ validace
@pytest.mark.parametrize(
    "entry",
    [
        "git.tailnet-name.ts.net",
        "*.ts.net",
        "100.101.102.103",
        "fd7a:115c:a1e0::1",
        "[fd7a:115c:a1e0::1]",
        "100.64.0.0/10",
        "fd7a:115c:a1e0::/48",
        "Žluťoučký.cz",
    ],
)
def test_valid_entries(entry: str) -> None:
    assert parse_config({"allow_http_hosts": [entry]}).allow_http_hosts == [entry]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("*", "všechny hosty"),
        ("*.net", "celé TLD"),
        ("git.*.net", "jen jako prefix"),
        ("host.example:3000", "port se neuvádí"),
        ("http://host.example", "jen host"),
        ("0.0.0.0/0", "celý internet"),
        ("100.64.0.0/33", "CIDR"),
        ("100.1", "neplatný"),
        ("0x64.1.1.1", "neplatný"),
        ("bad_host!", "neplatný"),
        ("", "prázdná"),
    ],
)
def test_invalid_entries(entry: str, message: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_config({"allow_http_hosts": [entry]})
    assert message in str(exc.value)
    assert "allow_http_hosts" in str(exc.value)


def test_default_is_empty_and_repo_overrides_cannot_set_it(tmp_path: Path) -> None:
    from repo_doctor.config import load_repo_overrides

    assert Config().allow_http_hosts == []
    (tmp_path / ".repo-doctor.toml").write_text('allow_http_hosts = ["*.ts.net"]\n')
    with pytest.raises(ConfigError):
        load_repo_overrides(tmp_path)


# ------------------------------------------------------------------ párování
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("git.x.ts.net", True),
        ("GIT.X.TS.NET", True),
        ("git.x.ts.net.", True),
        ("ts.net", False),
        ("evil-ts.net", False),
        ("ts.net.evil.com", False),
        ("100.101.102.103", True),
        ("100.128.0.1", False),
        ("100.64.0.0", True),
        ("0x64.0x65.1.1", False),
        ("100.101", False),
        ("[fd7a:115c:a1e0::5]", True),
        ("fd7a:115c:a1e1::5", False),
        ("žluťoučký.cz", True),
        ("ŽLUŤOUČKÝ.CZ", True),
        ("xn--luouk-uva4it5a4g.cz", True),
        ("", False),
    ],
)
def test_host_matching(host: str, expected: bool) -> None:
    policy = HttpHostPolicy(["*.ts.net", "100.64.0.0/10", "fd7a:115c:a1e0::/48", "žluťoučký.cz"])
    assert policy.allows_host(host) is expected


def test_rule_kinds() -> None:
    assert parse_rule("*.ts.net").kind == "suffix"
    assert parse_rule("100.64.0.0/10").kind == "net"
    assert parse_rule("::1").kind == "ip"
    assert parse_rule("Git.Example.").kind == "host"


def test_http_allowed_for_needs_all_http_urls_on_list() -> None:
    netpolicy.configure(["*.ts.net"])
    assert http_allowed_for(["http://git.a.ts.net/x.git"])
    assert http_allowed_for(["http://git.a.ts.net/x.git", "https://github.com/o/r.git"])
    assert not http_allowed_for(["http://git.a.ts.net/x.git", "http://evil.example/x.git"])
    assert not http_allowed_for(["https://github.com/o/r.git"])  # bez http nic nepřidávat
    assert not http_allowed_for([])


# ------------------------------------------------------------------ fetch přes http
@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")


@pytest.fixture
def served(tmp_path: Path, no_proxy: None) -> Iterator[tuple[GitHttpServer, RepoBuilder]]:
    """Bare upstream servírovaný přes http a lokální klon s novým commitem k fetchnutí."""
    srv_root = tmp_path / "srv"
    srv_root.mkdir()
    work = RepoBuilder.create(tmp_path / "work")
    work.write("a.txt", "a").commit("init")
    git(srv_root, "clone", "-q", "--bare", str(work.path), "up.git")
    victim = RepoBuilder.create(tmp_path / "projekty" / "obet")
    git(victim.path, "fetch", "-q", str(srv_root / "up.git"), "main")
    git(victim.path, "reset", "-q", "--hard", "FETCH_HEAD")
    work.write("b.txt", "b").commit("druhý")
    git(work.path, "push", "-q", str(srv_root / "up.git"), "main")
    with git_http_server(srv_root) as server:
        victim.remote("origin", server.url("up.git"))
        yield server, victim


def _upstream_head(server: GitHttpServer) -> str:
    return git(server.root / "up.git", "rev-parse", "main").strip()


def test_fetch_over_http_from_allowed_host(served: tuple[GitHttpServer, RepoBuilder]) -> None:
    server, victim = served
    netpolicy.configure(["127.0.0.1"])
    Git(victim.path).fetch(timeout=30)
    assert git(victim.path, "rev-parse", "origin/main").strip() == _upstream_head(server)


def test_fetch_over_http_refused_without_allowlist(
    served: tuple[GitHttpServer, RepoBuilder],
) -> None:
    server, victim = served
    with pytest.raises(GitError) as exc:
        Git(victim.path).fetch(timeout=30)
    assert server.requests == []  # nic neodešlo
    msg = explain_ssh_error(exc.value.stderr)
    assert msg is not None and "nepovolený protokol http" in msg
    assert "allow_http_hosts" in msg and "bez portu a bez http://" in msg


def test_second_remote_on_other_host_disables_http(
    served: tuple[GitHttpServer, RepoBuilder],
) -> None:
    server, victim = served
    netpolicy.configure(["127.0.0.1"])
    victim.remote("zaloha", f"http://nepovoleny.example:{server.port}/up.git")
    with pytest.raises(GitError) as exc:
        Git(victim.path).fetch(timeout=30)
    assert "transport 'http' not allowed" in exc.value.stderr
    assert server.requests == []


def test_legacy_remotes_file_is_evaluated(
    served: tuple[GitHttpServer, RepoBuilder],
) -> None:
    server, victim = served
    netpolicy.configure(["127.0.0.1"])
    victim.git("remote", "rename", "origin", "tail")
    # fetch bez argumentu jde na „origin“ – tady definovaný jen starým souborem .git/remotes
    legacy = victim.path / ".git" / "remotes"
    legacy.mkdir()
    (legacy / "origin").write_text(f"URL: http://localhost:{server.port}/up.git\n")
    assert f"http://localhost:{server.port}/up.git" in Git(victim.path).fetch_urls()
    with pytest.raises(GitError):
        Git(victim.path).fetch(timeout=30)
    assert server.requests == []


def test_insteadof_rewrite_to_other_host_is_evaluated(
    served: tuple[GitHttpServer, RepoBuilder],
) -> None:
    server, victim = served
    netpolicy.configure(["127.0.0.1"])
    # lokální konfigurace cizího repa přepíše povolenou URL na nepovolený host
    git(
        victim.path,
        "config",
        f"url.http://localhost:{server.port}/.insteadOf",
        f"http://127.0.0.1:{server.port}/",
    )
    with pytest.raises(GitError):
        Git(victim.path).fetch(timeout=30)
    assert server.requests == []


def test_redirect_from_allowed_to_other_host_is_refused(
    served: tuple[GitHttpServer, RepoBuilder], tmp_path: Path
) -> None:
    server, victim = served
    netpolicy.configure(["127.0.0.1"])
    with git_http_server(server.root) as other:
        server.redirect_to = f"http://localhost:{other.port}"
        with pytest.raises(GitError):
            Git(victim.path).fetch(timeout=30)
        assert server.requests  # první požadavek odešel na povolený host
        assert other.requests == []  # přesměrování na jiný host se nenásledovalo


def test_local_followredirects_cannot_reenable_redirects(
    served: tuple[GitHttpServer, RepoBuilder],
) -> None:
    server, victim = served
    netpolicy.configure(["127.0.0.1"])
    git(victim.path, "config", "http.followRedirects", "true")
    git(victim.path, "config", f"http.http://127.0.0.1:{server.port}/.followRedirects", "true")
    with git_http_server(server.root) as other:
        server.redirect_to = f"http://localhost:{other.port}"
        with pytest.raises(GitError):
            Git(victim.path).fetch(timeout=30)
        assert other.requests == []


def test_repo_doctor_toml_cannot_allow_http(
    served: tuple[GitHttpServer, RepoBuilder], capsys: pytest.CaptureFixture[str]
) -> None:
    server, victim = served
    (victim.path / ".repo-doctor.toml").write_text('allow_http_hosts = ["127.0.0.1"]\n')
    cli.main(["scan", str(victim.path.parent), "--fetch", "--report", "json"])
    err = capsys.readouterr().err
    assert "nepovolený protokol http" in err
    assert server.requests == []


def test_clone_and_ls_remote_follow_policy(
    served: tuple[GitHttpServer, RepoBuilder], tmp_path: Path
) -> None:
    server, _ = served
    url = server.url("up.git")
    with pytest.raises(GitError):
        Git.ls_remote(url)
    netpolicy.configure(["127.0.0.0/8"])
    Git.ls_remote(url)
    Git.clone(url, tmp_path / "klon")
    assert (tmp_path / "klon" / "b.txt").exists()


def test_scan_fetch_cli_with_allowlist_in_config(
    served: tuple[GitHttpServer, RepoBuilder], capsys: pytest.CaptureFixture[str]
) -> None:
    server, victim = served
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text('allow_http_hosts = ["127.0.0.1"]\n')
    cli.main(["scan", str(victim.path.parent), "--fetch", "--report", "json"])
    assert "fetch obet" not in capsys.readouterr().err
    assert git(victim.path, "rev-parse", "origin/main").strip() == _upstream_head(server)


# ------------------------------------------------------------------ HTTP API hostingů
def _forgejo(url: str) -> ForgeConfig:
    return ForgeConfig(name="domaci", type="forgejo", url=url, token_env="TEST_FORGE_TOKEN")


def test_insecure_forge_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FORGE_TOKEN", "tok-" + "x" * 20)
    fc = _forgejo("http://git.tailnet.ts.net:3000")
    assert insecure_reason(fc) is not None
    # návod nese klávesu z keymapu a hodnotu k vložení (#68), bez klávesy odkazuje na config.toml
    with_key = insecure_reason(fc, "4")
    assert with_key is not None and "v Nastavení (klávesa 4)" in with_key
    assert "host git.tailnet.ts.net" in with_key and "git.tailnet.ts.net:3000" not in with_key
    assert "v Nastavení nebo v config.toml" in (insecure_reason(fc) or "")
    mgr = ForgeManager.from_config(Config(forges=[fc]))
    assert "allow_http_hosts" in mgr.unavailable["domaci"]
    netpolicy.configure(["*.ts.net"])
    assert insecure_reason(fc) is None
    assert insecure_reason(_forgejo("https://git.example.com")) is None


@respx.mock
async def test_token_only_to_allowed_http_host() -> None:
    netpolicy.configure(["git.tailnet.ts.net"])
    token = "tok-" + "y" * 20
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"login": "ja"})

    respx.get("http://git.tailnet.ts.net:3000/api/v1/user").mock(side_effect=record)
    forge = build_forge(_forgejo("http://git.tailnet.ts.net:3000"), token=token)
    try:
        await forge.client.get_json("/user")
    finally:
        await forge.client.aclose()
    assert seen and seen[0].headers["authorization"] == f"token {token}"


@respx.mock
async def test_http_request_to_other_host_is_blocked_before_sending() -> None:
    netpolicy.configure(["git.tailnet.ts.net"])
    route = respx.get("http://evil.example/api").respond(200, json={})
    client = HttpClient(base_url="http://evil.example", headers={"Authorization": "token tajne"})
    try:
        with pytest.raises(InsecureHostError):
            await client.get_json("/api")
    finally:
        await client.aclose()
    assert not route.called


@respx.mock
async def test_redirect_to_unlisted_http_host_is_blocked() -> None:
    netpolicy.configure(["git.tailnet.ts.net"])
    respx.get("http://git.tailnet.ts.net/api/v1/user").respond(
        302, headers={"Location": "http://evil.example/steal"}
    )
    evil = respx.get("http://evil.example/steal").respond(200, json={})
    client = HttpClient(
        base_url="http://git.tailnet.ts.net/api/v1", headers={"Authorization": "token tajne"}
    )
    try:
        with pytest.raises(InsecureHostError):
            await client.get_json("/user")
    finally:
        await client.aclose()
    assert not evil.called


def test_forges_test_cli_reports_insecure_forge(capsys: pytest.CaptureFixture[str]) -> None:
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        '[[forges]]\nname = "domaci"\ntype = "forgejo"\nurl = "http://git.tailnet.ts.net:3000"\n'
    )
    assert cli.main(["forges", "test"]) == 1
    out = capsys.readouterr().out
    assert "✗ domaci" in out and "allow_http_hosts" in out


# ------------------------------------------------------------------ TUI
async def test_wizard_offers_http_allow_with_risk_explanation(tmp_path: Path) -> None:
    from repo_doctor.config import ConfigStore
    from repo_doctor.tui.screens.dialogs import ConfirmDialog
    from repo_doctor.tui.screens.forms import ForgeDialog
    from repo_doctor.tui.screens.wizard import WizardScreen
    from tests.sample import sample_result
    from tests.tui_helpers import fake_factory, make_app

    root = tmp_path / "repa"
    RepoBuilder.create(root / "r1")
    app = make_app(tmp_path, config=None, scanner_factory=fake_factory(sample_result().repos[:1]))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        wizard = app.screen
        assert isinstance(wizard, WizardScreen)
        wizard.query_one("#path").value = str(root)  # type: ignore[attr-defined]
        await pilot.press("ctrl+n")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ForgeDialog)
        dialog.query_one("#type").value = "forgejo"  # type: ignore[attr-defined]
        dialog.query_one("#name").value = "domaci"  # type: ignore[attr-defined]
        dialog.query_one("#url").value = "http://git.tailnet.ts.net:3000"  # type: ignore[attr-defined]
        await pilot.pause()
        dialog.action_save()
        await pilot.pause()
        confirm = app.screen
        assert isinstance(confirm, ConfirmDialog)
        body = str(confirm.body)
        assert "bez šifrování" in body and "Tailscale" in body
        assert app.focused is not None and app.focused.id == "cancel"  # výchozí volba Zrušit
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert isinstance(app.screen, WizardScreen)
        await pilot.press("ctrl+s")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
    cfg = ConfigStore(tmp_path / "config.toml").load()
    assert cfg.allow_http_hosts == ["git.tailnet.ts.net"]
    assert cfg.forges[0].url == "http://git.tailnet.ts.net:3000"


async def test_forges_screen_rejects_unlisted_http_and_notes_allowed(tmp_path: Path) -> None:
    from repo_doctor.tui.screens.dialogs import ConfirmDialog
    from repo_doctor.tui.screens.forms import ForgeDialog
    from tests.tui_helpers import make_app

    app = make_app(tmp_path, config="")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.press("n")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ForgeDialog)
        dialog.query_one("#type").value = "gitea"  # type: ignore[attr-defined]
        dialog.query_one("#url").value = "http://git.tailnet.ts.net"  # type: ignore[attr-defined]
        await pilot.pause()
        dialog.action_save()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)  # i tady nabídka povolit host (#68)
        await pilot.press("enter")  # výchozí volba je Zrušit
        await pilot.pause()
        assert app.screen_stack[-1] is dialog  # neuloženo
        error = str(dialog.query_one("#error").render())
        # návod: kde (klávesa z keymapu), jak se pole jmenuje v UI i v configu a co vložit
        assert "allow_http_hosts" in error and "Hosty s povoleným nešifrovaným http" in error
        assert f"(klávesa {app.key('screen_settings')})" in error
        assert "host git.tailnet.ts.net" in error and "bez portu" in error
        await pilot.press("escape")
    assert app.config.forges == []
    assert app.config.allow_http_hosts == []

    cfg = (
        'allow_http_hosts = ["*.ts.net"]\n'
        '[[forges]]\nname = "domaci"\ntype = "gitea"\nurl = "http://git.tailnet.ts.net"\n'
    )
    app = make_app(tmp_path, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        detail = str(app.screen.query_one("#forge-detail").render())
        assert "nešifrované spojení, povoleno v allow_http_hosts" in detail


async def test_forges_screen_add_dialog_allows_http_host_after_confirmation(
    tmp_path: Path,
) -> None:
    from repo_doctor.config import ConfigStore
    from repo_doctor.tui.screens.dialogs import ConfirmDialog
    from repo_doctor.tui.screens.forms import ForgeDialog
    from tests.tui_helpers import make_app

    app = make_app(tmp_path, config="")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.press("n")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ForgeDialog)
        dialog.query_one("#type").value = "forgejo"  # type: ignore[attr-defined]
        dialog.query_one("#name").value = "domaci"  # type: ignore[attr-defined]
        dialog.query_one("#url").value = "http://100.101.102.103:3000"  # type: ignore[attr-defined]
        await pilot.pause()
        dialog.action_save()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("tab", "enter")  # Povolit http pro tento host
        await pilot.pause()
        assert not isinstance(app.screen, ForgeDialog)
    assert app.config.allow_http_hosts == ["100.101.102.103"]  # jen host, bez portu
    assert [f.name for f in app.config.forges] == ["domaci"]
    assert netpolicy.policy().allows_host("100.101.102.103")
    cfg = ConfigStore(tmp_path / "config.toml").load()
    assert cfg.allow_http_hosts == ["100.101.102.103"]


async def test_settings_validates_allow_http_hosts(tmp_path: Path) -> None:
    from textual.widgets import Input

    from tests.tui_helpers import make_app

    app = make_app(tmp_path, config="")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        field = app.screen.query_one("#allow_http_hosts", Input)
        field.value = "*"
        await pilot.pause()
        assert "všechny hosty" in str(app.screen.query_one("#settings-error").render())
        assert app.config.allow_http_hosts == []
        field.value = "*.ts.net, 100.64.0.0/10"
        await pilot.pause()
        assert app.config.allow_http_hosts == ["*.ts.net", "100.64.0.0/10"]
        assert netpolicy.policy().allows_host("git.x.ts.net")
