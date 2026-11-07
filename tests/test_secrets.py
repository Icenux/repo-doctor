from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from repo_doctor import secrets_scan
from repo_doctor.checks import SkipCheck, get_check
from repo_doctor.checks.secrets import is_sensitive_file
from repo_doctor.config import Config, ConfigError, LimitsConfig, RootConfig, load_repo_overrides
from repo_doctor.forges.urls import SshConfig
from repo_doctor.gitwrap import Git, GitTimeout
from repo_doctor.reports import html, jsonreport, markdown
from repo_doctor.scanner import Scanner, ScanOptions
from repo_doctor.secrets_scan import entropy, is_excluded_path, parse_log_patch, scan_text
from tests import factory as fx
from tests.factory import RepoBuilder
from tests.helpers import ctx, run
from tests.sample import NOW

ALL_FAKES = {
    "aws-access-key": lambda: f"key = {fx.fake_aws_key()}",
    "github-token": lambda: f"GH={fx.fake_github_token()}",
    "gitlab-token": lambda: f"t: {fx.fake_gitlab_token()}",
    "slack-token": lambda: f"slack {fx.fake_slack_token()}",
    "openai-key": lambda: f"OPENAI_API_KEY={fx.fake_openai_key()}",
    "anthropic-key": lambda: f"ANTHROPIC_API_KEY={fx.fake_anthropic_key()}",
    "private-key": lambda: fx.fake_private_key().splitlines()[0],
    "jwt": lambda: f"auth: {fx.fake_jwt()}",
    "connection-string": lambda: f"DATABASE_URL={fx.fake_db_url()}",
    "generic-secret": lambda: f'api_key = "{fx.fake_generic_secret()}"',
}


@pytest.mark.parametrize("rule", sorted(ALL_FAKES))
def test_rules_detect_and_mask(rule: str) -> None:
    line = ALL_FAKES[rule]()
    (m,) = scan_text(line, "config/app.env")
    assert m.rule == rule
    assert "…(" in m.masked
    # žádná část delší než 4 znaky z tajemství se neobjeví v maskované podobě
    assert len(m.masked.split("…")[0]) <= 4


@pytest.mark.parametrize(
    "line",
    [
        "key = AKIAIOSFODNN7EXAMPLE",
        'password = "changeme-please-now"',
        'token = "${GITHUB_TOKEN_FROM_ENV}"',
        'password = "aaaaaaaaaaaaaaaa"',
        "postgres://user:${DB_PASS}@host/db",
        'api_key = "short"',
        f"x = {fx.fake_aws_key()}  # gitleaks:allow",
        'secret = "os.environ.get(KEY_NAME)"',
    ],
)
def test_negatives(line: str) -> None:
    assert scan_text(line, "app.py") == []


SEQ_DIGITS = "".join(str(i) for i in range(10))
SEQ_UP = "abcdefghijklmnopqrstuvwxyz"
SEQ_DOWN = SEQ_UP[::-1]
SEQ_MIXED = "".join(c.upper() if i % 2 == 0 else c for i, c in enumerate(SEQ_UP))  # AbCdEf…


@pytest.mark.parametrize(
    "value",
    [
        "sk_" + "live_" + SEQ_MIXED[:18],  # Stripe: prefix nesmí ředit řadu
        "AI" + "zaSyA-" + SEQ_DIGITS + SEQ_UP[:21],
        "gl" + "pat-" + SEQ_MIXED[:20],
        "abcdef123456",
        "9f8e7d6c5b4a",  # dvě řady přes sebe (9876… a fedc…)
        "0a1b2c3d4e5f",
        "z" * 10,
        "abc...xyz",
    ],
)
def test_is_obvious_fake_recognizes_made_up_values(value: str) -> None:
    assert secrets_scan.is_obvious_fake(value)


@pytest.mark.parametrize(
    "value",
    [
        "Company123456",  # skutečné slabé heslo s řadou na konci
        "hunter2secret",
        "wrong-key-that-does-not-match",
        "a3F9kQ2xZ7mP4wL8",
        "sk_" + "live_" + "q8W3e7R2t5Y9u1I4o6P0",
        fx.fake_github_token(),
        fx.fake_generic_secret(),
    ],
)
def test_is_obvious_fake_keeps_real_looking_values(value: str) -> None:
    assert not secrets_scan.is_obvious_fake(value)


@pytest.mark.parametrize(
    "line",
    [
        'api_key = "123..."',
        'api_key = "abc..."',
        'api_key = "zxyx..."',
        "OPENAI_API_KEY=" + "sk-" + "abc...xyz",
        'api_key = "' + SEQ_DIGITS + SEQ_UP[:16] + '"',
        'token: "' + SEQ_UP + '"',
        'secret = "' + SEQ_DOWN + '"',
        'api_key = "' + "sk" + "-" + SEQ_UP + SEQ_DIGITS + '"',
        "key = " + "AK" + "IA" + SEQ_DIGITS + "ABCDEF",
        "GH=" + "gh" + "p_" + SEQ_DIGITS * 4,
        'token = "' + "zxyx" * 6 + '"',
        "export API_KEY=" + SEQ_DIGITS * 2,
        # pevný prefix (sk-ant-api03-, glpat-, xoxb-…) nesmí ředit řadu v těle hodnoty (#90)
        "ANTHROPIC_API_KEY=" + "sk-" + "ant-" + "api03-" + SEQ_MIXED + SEQ_DIGITS[:6],
        "OPENAI_API_KEY=" + "sk-" + "proj-" + SEQ_MIXED + "01",
        "t: " + "gl" + "pat-" + SEQ_MIXED[:20],
        "slack " + "xo" + "xb-" + SEQ_DIGITS + "-" + SEQ_UP[:10],
    ],
)
def test_obviously_fake_values_are_not_findings(line: str) -> None:
    assert scan_text(line, "docs/config.yml") == []


def test_fake_filter_keeps_realistic_values() -> None:
    # náhodné hodnoty z továrny se nesmí tvářit jako sekvence
    for rule in ("aws-access-key", "github-token", "openai-key", "generic-secret"):
        assert scan_text(ALL_FAKES[rule](), "config/app.env"), rule


@pytest.mark.parametrize(
    "line",
    [
        'password = "' + "Qwerty" + "123456" + '"',
        'password = "' + "Company" + "123456" + '"',
        'api_key = "' + "Company" + "123456789" + '"',
        "DATABASE_URL=postgres://app:" + "Company" + "123456" + "@db/app",
        "DATABASE_URL=postgres://app:" + "Qwerty" + "123456" + "@db/app",
        'secret_key = "some-long-text-with-' + "abc...xyz" + '-inside"',
        # prefix se odřezává jen u klíčů s pevným prefixem vendora, ne u obecných hesel (#90)
        'password = "' + "Company" + "_123456" + '"',
        'api_key = "' + "Prod" + "-" + SEQ_UP[:12] + '"',
        "DATABASE_URL=postgres://app:" + "Company" + "_123456" + "@db/app",
    ],
)
def test_partial_sequences_are_still_findings(line: str) -> None:
    assert scan_text(line, "config/app.env"), line


def test_sequence_share_and_periodic() -> None:
    assert secrets_scan._sequence_share(SEQ_DIGITS) == 1.0
    assert secrets_scan._sequence_share("QPONMLk") == 1.0
    assert secrets_scan._sequence_share("9QPONMLK") == pytest.approx(7 / 8)
    assert secrets_scan._sequence_share("12345") == 0.0  # pět znaků ještě není řada
    assert secrets_scan._sequence_share("abc123def456") == 0.0
    assert secrets_scan._sequence_share("Company123456") < secrets_scan.SEQUENCE_SHARE
    assert secrets_scan._sequence_share("9a" * 10) == 0.0
    assert secrets_scan._is_periodic("zxyx" * 3)
    assert secrets_scan._is_periodic("ab" * 5 + "a")
    assert not secrets_scan._is_periodic("abcde")
    assert not secrets_scan._is_periodic("k3Jd9sLq2Xv8")


def test_excluded_paths() -> None:
    assert is_excluded_path("package-lock.json")
    assert is_excluded_path("tests/fixtures/keys.txt")
    assert is_excluded_path("a/__snapshots__/x.ambr") is True
    assert not is_excluded_path("src/config.py")
    assert scan_text(f"k={fx.fake_aws_key()}", "yarn.lock") == []
    assert scan_text("x" * 5000 + fx.fake_aws_key(), "a.js") == []


def test_entropy() -> None:
    assert entropy("") == 0
    assert entropy("aaaa") == 0
    assert entropy(fx.fake_generic_secret()) > 4


def test_parse_log_patch() -> None:
    key = fx.fake_aws_key()
    lines = [
        "\0commit abcdef1234567 1700000000",
        "diff --git a/x b/x",
        "--- /dev/null",
        "+++ b/config/prod.env",
        "@@ -0,0 +1,4 @@",
        "+A=1",
        "+B=2",
        "+C=3",
        f"+AWS={key}",
        "\0commit 999 1600000000",
        "+++ /dev/null",
        f"+AWS={key}",
        "+++ b/package-lock.json",
        f"+{key}",
    ]
    (m,) = list(parse_log_patch(lines))
    assert m.commit == "abcdef1234567"
    assert m.line == 4
    assert m.path == "config/prod.env"
    assert m.commit_ts == 1_700_000_000


def test_secrets_tree_and_history(tmp_path: Path) -> None:
    key = fx.fake_aws_key()
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("config/prod.env", f"A=1\nB=2\nC=3\nAWS_ACCESS_KEY_ID={key}\n").commit("add")
    rb.remove("config/prod.env").write("README.md", "x").commit("rm")
    rb.write("app.py", f'token = "{fx.fake_github_token()}"\n')  # nesledovaný, neignorovaný
    tree = run("secrets-tree", rb.path)
    assert [f.location.path for f in tree] == ["app.py"]
    hist = run("secrets-history", rb.path)
    assert len(hist) == 1
    f = hist[0]
    assert f.location.path == "config/prod.env"
    assert f.location.line == 4
    assert f.snippet == key[:4] + "…(20 znaků)"
    assert key not in f.model_dump_json()
    assert "a" in (f.location.commit or "") or f.location.commit
    assert f.kind == "AWS access key"


def test_secrets_negative_repo(tmp_path: Path) -> None:
    rb = fx.healthy_python_repo(tmp_path / "ok")
    assert run("secrets-tree", rb.path) == []
    assert run("secrets-history", rb.path) == []
    empty = RepoBuilder.create(tmp_path / "empty")
    assert run("secrets-history", empty.path) == []


def test_history_timeout_marks_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("a.txt", "x").commit()

    def boom(self: Git, *a: str, timeout: float | None = None) -> Any:
        raise GitTimeout(a, 1)

    monkeypatch.setattr(Git, "stream_lines", boom)
    (f,) = run("secrets-history", rb.path, Config(limits=LimitsConfig(history_timeout_s=1)))
    assert f.check_id == "scan-incomplete"


def _fake_gitleaks(
    monkeypatch: pytest.MonkeyPatch, report: list[dict[str, Any]], *, timeout: bool = False
) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(secrets_scan, "gitleaks_available", lambda: True)
    monkeypatch.setattr("repo_doctor.checks.secrets.gitleaks_available", lambda: True)

    def fake_run(cmd: list[str], limit: float, env: dict[str, str] | None = None) -> None:
        calls.append(cmd)
        if timeout:
            raise subprocess.TimeoutExpired(cmd, 1)
        out = Path(cmd[cmd.index("--report-path") + 1])
        out.write_text(json.dumps(report))

    monkeypatch.setattr(secrets_scan, "_exec", fake_run)
    return calls


def test_gitleaks_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("app.env", "x").commit()
    secret = fx.fake_github_token()
    report: list[Any] = [
        {
            "RuleID": "github-pat",
            "Description": "GitHub PAT",
            "File": str(rb.path) + "/app.env",
            "StartLine": 3,
            "Secret": secret,
            "Match": "x",
            "Commit": "abc1234",
            "Date": "2026-03-14T10:00:00Z",
        },
        {"RuleID": "x", "File": "untracked-elsewhere.txt", "Secret": secret, "Date": "garbage"},
    ]
    calls = _fake_gitleaks(monkeypatch, report)
    tree = run("secrets-tree", rb.path)
    assert len(tree) == 1 and tree[0].snippet == secret[:4] + "…(40 znaků)"
    assert "--no-git" in calls[0]
    hist = run("secrets-history", rb.path)
    assert len(hist) == 1  # deduplikace podle otisku
    assert "--no-git" not in calls[1]
    assert secret not in json.dumps([f.model_dump(mode="json") for f in tree + hist])


def _leak(rule: str, secret: str, rb: RepoBuilder, line: int = 3, **extra: Any) -> dict[str, Any]:
    return {
        "RuleID": rule,
        "Description": rule,
        "File": str(rb.path) + "/app.env",
        "StartLine": line,
        "Secret": secret,
        "Commit": "abc1234",
        "Date": "2026-03-14T10:00:00Z",
        **extra,
    }


def _fakes_report(rb: RepoBuilder) -> tuple[list[Any], str]:
    real = fx.fake_github_token()
    report = [
        _leak("stripe-access-token", "sk_" + "live_" + SEQ_MIXED[:18], rb, 1),
        _leak("gcp-api-key", "AI" + "zaSyA-" + SEQ_DIGITS + SEQ_UP[:21], rb, 2),
        _leak("gitlab-pat", "gl" + "pat-" + SEQ_MIXED[:20], rb, 3),
        _leak("generic-api-key", "9f8e7d6c5b4a", rb, 4),
        _leak("generic-api-key", "Company123456", rb, 5),  # skutečné slabé heslo zůstává
        _leak("github-pat", real, rb, 6),
    ]
    return report, real


def test_gitleaks_obvious_fakes_are_hidden_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("app.env", "x").commit()
    report, _real = _fakes_report(rb)
    _fake_gitleaks(monkeypatch, report)
    c = ctx(rb.path)
    tree = get_check("secrets-tree").run(c)  # type: ignore[union-attr]
    hist = get_check("secrets-history").run(c)  # type: ignore[union-attr]
    # zůstalo slabé heslo a náhodný token; čtyři vymyšlené hodnoty zmizely
    assert sorted(f.location.line or 0 for f in tree) == [5, 6]
    assert sorted(f.location.line or 0 for f in hist) == [5, 6]
    # strom i historie hlásí tytéž hodnoty – počítají se jednou
    assert len(c.hidden_fakes) == 4


def test_gitleaks_private_key_is_never_judged_as_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("app.env", "x").commit()
    _fake_gitleaks(monkeypatch, [_leak("private-key", "z" * 40, rb)])
    assert len(run("secrets-tree", rb.path)) == 1


def test_gitleaks_fakes_stay_when_filter_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("app.env", "x").commit()
    report, _real = _fakes_report(rb)
    _fake_gitleaks(monkeypatch, report)
    c = ctx(rb.path, Config(hide_fake_secrets=False))
    assert len(get_check("secrets-tree").run(c)) == 6  # type: ignore[union-attr]
    assert c.hidden_fakes == set()


def test_repo_config_cannot_turn_the_filter_on_or_off(tmp_path: Path) -> None:
    (tmp_path / ".repo-doctor.toml").write_text("hide_fake_secrets = false\n")
    with pytest.raises(ConfigError):
        load_repo_overrides(tmp_path)


async def test_scan_reports_hidden_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rb = RepoBuilder.create(tmp_path / "projekty" / "r")
    rb.write("app.env", "x").commit()
    report, _real = _fakes_report(rb)
    _fake_gitleaks(monkeypatch, report)
    cfg = Config(roots=[RootConfig(path=str(tmp_path / "projekty"))])
    result = await Scanner(cfg, ScanOptions(offline=True, now=NOW), ssh_config=SshConfig()).run()
    (repo,) = result.repos
    assert repo.hidden_fakes == 4
    assert "vymyšlené hodnoty: 4" in markdown.render(result)
    assert "vymyšlené hodnoty: 4" in html.render(result)
    assert json.loads(jsonreport.render(result))["repos"][0]["hidden_fakes"] == 4


def test_gitleaks_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("a", "x").commit()
    _fake_gitleaks(monkeypatch, report=[], timeout=True)
    assert run("secrets-tree", rb.path)[0].check_id == "scan-incomplete"
    assert run("secrets-history", rb.path)[0].check_id == "scan-incomplete"


def test_gitleaks_bad_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_scan, "_exec", lambda cmd, limit, env=None: None)
    assert secrets_scan.run_gitleaks(tmp_path, history=False, timeout=1) == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", True),
        ("app/.env.production", True),
        (".env.example", False),
        ("id_rsa", True),
        ("certs/server.key", True),
        ("certs/ca.pem", False),
        ("certs/private.pem", True),
        ("credentials.json", True),
        ("config/prod.env", True),
        ("README.md", False),
        ("docs/.env.sample", False),
    ],
)
def test_sensitive_files(path: str, expected: bool) -> None:
    assert is_sensitive_file(path) is expected


def test_env_committed(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write(".env", "A=1").write(".env.example", "A=").write("keys/id_rsa", "x").commit()
    findings = run("env-committed", rb.path)
    assert sorted(f.location.path or "" for f in findings) == [".env", "keys/id_rsa"]
    assert all(f.fixable for f in findings)
    assert run("env-committed", fx.healthy_python_repo(tmp_path / "ok").path) == []


def test_public_sensitive(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("hosts.yml", "web:\n  ansible_host: 100.101.102.103\n")
    rb.write("config.yaml", "db: 192.168.1.20\n")
    rb.write("docs.md", "ukázka 10.0.0.1\n")
    rb.write("public.json", '{"dns": "8.8.8.8", "bind": "0.0.0.0", "lo": "127.0.0.1"}')
    rb.write("app.toml", 'url = "https://git.tail1234.ts.net"\n')
    rb.write("infra/terraform.tfstate", "{}").commit()
    with pytest.raises(SkipCheck):
        run("public-sensitive", rb.path)
    assert run("public-sensitive", rb.path, visibility="private") == []
    findings = {f.location.path: f for f in run("public-sensitive", rb.path, visibility="public")}
    assert set(findings) == {"hosts.yml", "config.yaml", "app.toml", "infra/terraform.tfstate"}
    assert "Tailscale" in findings["hosts.yml"].message
    assert "ts.net" in findings["app.toml"].message


DOCKERFILE = """\
FROM python AS base
FROM node:latest
FROM base
FROM alpine@sha256:abc
FROM ${IMAGE}
FROM --platform=linux/amd64 debian:12 AS final
ENV API_TOKEN=Zx81kLmQ20pRtY7w
ENV PATH=/usr/bin DEBUG=1
ENV OLD_PASSWORD s3cr3tVALUE991
ARG GITHUB_TOKEN
ADD https://example.com/x.tgz /tmp/
RUN apt-get update && \\
    apt-get install -y curl
# komentář
"""


def test_docker_hygiene(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("Dockerfile", DOCKERFILE).commit()
    findings = run("docker-hygiene", rb.path)
    msgs = " | ".join(f.message for f in findings)
    assert "FROM python – bez tagu" in msgs
    assert "FROM node:latest – tag :latest" in msgs
    assert "FROM base" not in msgs and "alpine" not in msgs and "IMAGE" not in msgs
    assert "ENV API_TOKEN" in msgs and "ENV OLD_PASSWORD" in msgs
    assert "GITHUB_TOKEN" not in msgs and "PATH" not in msgs
    assert "ADD z URL" in msgs
    assert "pod rootem" in msgs
    assert any(f.key == "dockerignore" and f.fixable for f in findings)


def test_docker_negative(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("docker/app.Dockerfile", "FROM python:3.12-slim\nUSER app\n").write(
        ".dockerignore", ".git\n"
    ).commit()
    assert run("docker-hygiene", rb.path) == []
    rb2 = RepoBuilder.create(tmp_path / "r2")
    rb2.write("a", "a").commit()
    assert run("docker-hygiene", rb2.path) == []


def test_context_read_text(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("bin", b"\0\1\2").write("big", "x" * 3000).write("t", "text").commit()
    c = ctx(rb.path, Config(limits=LimitsConfig(max_file_kb=1)))
    assert c.read_text("bin") is None
    assert c.read_text("big") is None
    assert c.read_text("t") == "text"
    assert c.read_text("missing") is None
    assert c.has_any("T") and not c.has_any("nothing")


def test_fingerprint_is_slow_keyed_and_stable() -> None:
    import hashlib

    from repo_doctor.secrets_scan import fingerprint

    secret = fx.fake_generic_secret(21)
    assert fingerprint(secret) == fingerprint(secret)  # stabilní (allowlist)
    assert len(fingerprint(secret)) == 16
    assert fingerprint(secret) != hashlib.sha256(secret.encode()).hexdigest()[:16]
    assert fingerprint(secret) != fingerprint(secret + "x")
