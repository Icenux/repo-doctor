"""Vestavěný skener tajemství (regexy + entropie) a volitelné napojení na gitleaks.

Surová hodnota tajemství nikdy neopustí tento modul: výsledkem je `SecretMatch` s maskovanou
ukázkou a otiskem (sha256), podle kterého se nálezy deduplikují.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from pathlib import Path, PurePosixPath

from repo_doctor.gitwrap import Git, git_env, run_process
from repo_doctor.i18n import N_, _
from repo_doctor.masking import mask


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    pattern: re.Pattern[str]
    group: int = 0
    min_entropy: float | None = None
    keywords: tuple[str, ...] = ()
    config_only: bool = False  # jen v konfiguračních souborech (.env, yaml, ini…)


def _r(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


RULES: tuple[Rule, ...] = (
    Rule(
        "aws-access-key",
        "AWS access key",
        _r(r"\b((?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16})\b"),
        1,
        keywords=("AKIA", "ASIA", "ABIA", "ACCA"),
    ),
    Rule(
        "aws-secret-key",
        "AWS secret key",
        _r(
            r"(?i)aws_?secret_?(?:access_?)?key\W{0,5}[:=]\s*[\"']?([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"
        ),
        1,
        3.5,
        ("aws",),
    ),
    Rule(
        "github-token",
        "GitHub token",
        _r(r"\b(gh[pousr]_[A-Za-z0-9]{36,255})\b"),
        1,
        keywords=("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    ),
    Rule(
        "github-pat",
        "GitHub fine-grained token",
        _r(r"\b(github_pat_[A-Za-z0-9_]{60,255})\b"),
        1,
        keywords=("github_pat_",),
    ),
    Rule(
        "gitlab-token",
        "GitLab token",
        _r(r"\b(gl(?:pat|dt|rt|ptt|cbt)-[A-Za-z0-9_\-]{20,})"),
        1,
        keywords=("glpat-", "gldt-", "glrt-", "glptt-", "glcbt-"),
    ),
    Rule(
        "slack-token", "Slack token", _r(r"\b(xox[abposr]-[A-Za-z0-9-]{10,})"), 1, keywords=("xox",)
    ),
    Rule(
        "slack-webhook",
        "Slack webhook",
        _r(r"(https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{16,})"),
        1,
        keywords=("hooks.slack.com",),
    ),
    Rule(
        "anthropic-key",
        N_("Anthropic API klíč"),
        _r(r"\b(sk-ant-[A-Za-z0-9]+-[A-Za-z0-9_\-]{32,})"),
        1,
        keywords=("sk-ant-",),
    ),
    Rule(
        "openai-key",
        N_("OpenAI API klíč"),
        _r(r"\b(sk-(?:proj-|svcacct-|admin-)?(?!ant-)[A-Za-z0-9_\-]{32,})"),
        1,
        3.0,
        ("sk-",),
    ),
    Rule(
        "private-key",
        N_("Privátní klíč"),
        _r(r"(-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----)"),
        1,
        keywords=("PRIVATE KEY",),
    ),
    Rule(
        "jwt",
        "JWT",
        _r(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
        1,
        keywords=("eyJ",),
    ),
    Rule(
        "connection-string",
        N_("Connection string s heslem"),
        _r(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|mssql|sqlserver)://[^:/\s@\"']+:([^@\s\"'/]{4,})@"
        ),
        1,
        keywords=("://",),
    ),
    Rule(
        "generic-secret",
        N_("Obecné tajemství"),
        _r(
            r"(?i)(?<![A-Za-z0-9])(?:api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token|"
            r"auth[_-]?token|private[_-]?token|password|passwd|secret|token)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,200})[\"']"
        ),
        1,
        3.5,
        ("key", "secret", "token", "pass"),
    ),
    Rule(
        "generic-secret",
        N_("Obecné tajemství"),
        _r(
            r"(?i)^\s*(?:export\s+)?[\"']?[A-Za-z0-9_.-]*(?:api[_-]?key|apikey|secret|token|password|passwd)"
            r"[\"']?\s*[:=]\s*([^\"'\s#]{12,200})\s*(?:#.*)?$"
        ),
        1,
        3.5,
        ("key", "secret", "token", "pass"),
        config_only=True,
    ),
)

LOCKFILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
        "flake.lock",
        "bun.lockb",
    }
)
FIXTURE_DIRS = frozenset(
    {"fixtures", "__fixtures__", "testdata", "__snapshots__", "snapshots", "cassettes"}
)
SKIP_EXT = (".min.js", ".map", ".snap", ".svg", ".lock")
PLACEHOLDER = re.compile(
    r"(?i)((?<![a-z])(example|sample|dummy|placeholder|changeme|change_me|redacted|fake|test[_-]?key|todo|"
    r"replace[_-]?me)(?![a-z])|your[_-]|xxxx|\*\*\*\*|<[^>]*>|\$\{|\{\{)"
)
ALLOW_MARKERS = ("gitleaks:allow", "repo-doctor:allow")


@dataclass(frozen=True)
class SecretMatch:
    rule: str
    kind: str
    masked: str
    fingerprint: str
    path: str
    line: int
    commit: str | None = None
    commit_ts: int | None = None


def entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def is_excluded_path(path: str) -> bool:
    p = PurePosixPath(path)
    if p.name in LOCKFILES or p.name.endswith(SKIP_EXT):
        return True
    return any(part in FIXTURE_DIRS for part in p.parts[:-1])


SEQUENCE_RUN = 6  # tolik po sobě jdoucích znaků (123456, abcdef, zyxwvu) už není náhoda
MAX_PERIOD = 4  # opakující se vzor typu zxyxzxyx…


def _has_sequence(value: str) -> bool:
    """Obsahuje souvislou vzestupnou či sestupnou řadu písmen nebo číslic (bez rozlišení velikosti)."""
    lowered = value.lower()
    run = 1
    step = 0
    for prev, cur in pairwise(lowered):
        diff = ord(cur) - ord(prev)
        same_class = prev.isalnum() and cur.isalnum() and prev.isdigit() == cur.isdigit()
        if diff in (1, -1) and same_class:
            run = run + 1 if diff == step else 2
            step = diff
        else:
            run, step = 1, 0
        if run >= SEQUENCE_RUN:
            return True
    return False


def _is_periodic(value: str) -> bool:
    n = len(value)
    if n < 2 * MAX_PERIOD:
        return False
    for period in range(1, MAX_PERIOD + 1):
        if all(value[i] == value[i - period] for i in range(period, n)):
            return True
    return False


def _is_obvious_fake(value: str) -> bool:
    """Zjevně vymyšlená hodnota z dokumentace a testů: `sk-abc...xyz`, `1234567890`, `abcdefgh`, `zxyxzxyx`."""
    return "..." in value or "…" in value or _has_sequence(value) or _is_periodic(value)


def _looks_placeholder(value: str) -> bool:
    if PLACEHOLDER.search(value):
        return True
    return len(set(value)) <= 3 or _is_obvious_fake(value)


FINGERPRINT_SALT = b"repo-doctor/secret-fingerprint/v2"


@lru_cache(maxsize=2048)
def fingerprint(value: str) -> str:
    """Stabilní otisk tajemství (pro deduplikaci a allowlist).

    Používá scrypt s pevnou solí: otisk je stejný na všech strojích (allowlist
    v `.repo-doctor.toml` se commituje), ale ověřit kandidátní heslo stojí ~40 ms
    místo mikrosekund u holého SHA-256 – slovníkový útok na sdílený report se nevyplatí.
    """
    return hashlib.scrypt(value.encode(), salt=FINGERPRINT_SALT, n=2**14, r=8, p=1, dklen=8).hex()


CONFIG_FILE = re.compile(
    r"(?i)(^|/)(\.env[^/]*|[^/]*\.(env|ya?ml|ini|cfg|conf|properties|toml|tfvars))$"
)
# Placeholder filtr se týká jen pravidel bez pevného prefixu (u ghp_… apod. je shoda dost jistá).
PLACEHOLDER_RULES = {
    "generic-secret",
    "connection-string",
    "aws-access-key",
    "aws-secret-key",
    "openai-key",
}
# Hlavička privátního klíče je pevný text, ne hodnota k posouzení.
NO_FAKE_FILTER = {"private-key"}
DEFAULT_DB_PASSWORDS = {
    "postgres",
    "password",
    "root",
    "mysql",
    "admin",
    "secret",
    "example",
    "test",
    "guest",
}


def _first_match(rule: Rule, line: str) -> str | None:
    for m in rule.pattern.finditer(line):
        value = m.group(rule.group)
        if rule.id in PLACEHOLDER_RULES and _looks_placeholder(value):
            continue
        if rule.id not in NO_FAKE_FILTER and _is_obvious_fake(value):
            continue
        if rule.id == "connection-string" and value.lower() in DEFAULT_DB_PASSWORDS:
            continue
        if rule.min_entropy is not None and entropy(value) < rule.min_entropy:
            continue
        if rule.id == "generic-secret" and value.startswith(
            ("$", "%", "os.", "env.", "process.", "/")
        ):
            continue
        return value
    return None


def scan_line(line: str, path: str, lineno: int) -> Iterator[SecretMatch]:
    """Nejvýš jeden nález na řádek; specifická pravidla mají přednost před obecným."""
    if any(marker in line for marker in ALLOW_MARKERS):
        return
    lowered = line.lower()
    is_config = bool(CONFIG_FILE.search(path))
    for rule in RULES:
        if rule.config_only and not is_config:
            continue
        if rule.keywords and not any(k.lower() in lowered for k in rule.keywords):
            continue
        value = _first_match(rule, line)
        if value is not None:
            yield SecretMatch(
                rule=rule.id,
                kind=_(rule.kind),
                masked=mask(value),
                fingerprint=fingerprint(value),
                path=path,
                line=lineno,
            )
            return


def scan_text(text: str, path: str) -> list[SecretMatch]:
    if is_excluded_path(path):
        return []
    found: list[SecretMatch] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:  # minifikované řádky
            continue
        found.extend(scan_line(line, path, i))
    return found


@dataclass
class HistoryScan:
    matches: list[SecretMatch]
    complete: bool


def _header_path(target: str) -> str | None:
    target = target.rstrip("\t")  # git přidá TAB za názvy s mezerami
    if target == "/dev/null":
        return None
    if target.startswith('"') and target.endswith('"'):
        target = target[1:-1]
    return target[2:] if target.startswith("b/") else target


def parse_log_patch(lines: Iterable[str]) -> Iterator[SecretMatch]:
    """Parsuje `git log -p -U0 --format=%x00commit %H %ct` a skenuje přidané řádky.

    Hlavičky `---`/`+++` se berou jen mezi `diff --git` a prvním `@@` – přidaný řádek
    začínající na `++ ` by se jinak spletl s hlavičkou souboru.
    """
    commit: str | None = None
    commit_ts: int | None = None
    path: str | None = None
    lineno = 0
    in_header = False
    for raw in lines:
        if raw.startswith("\0commit "):
            parts = raw[8:].split()
            commit = parts[0] if parts else None
            commit_ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            path, in_header = None, False
            continue
        if raw.startswith("diff --git "):
            path, in_header = None, True
            continue
        if in_header:
            if raw.startswith("+++ "):
                path = _header_path(raw[4:])
                if path and is_excluded_path(path):
                    path = None
            elif raw.startswith("@@"):
                in_header = False
            if in_header:
                continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+") and path is not None:
            for match in scan_line(raw[1:], path, lineno):
                yield SecretMatch(**{**match.__dict__, "commit": commit, "commit_ts": commit_ts})
            lineno += 1


# ---------------------------------------------------------------------- gitleaks
def gitleaks_path() -> str | None:
    from repo_doctor.tools import find

    return find("gitleaks")


def gitleaks_available() -> bool:
    return gitleaks_path() is not None


def _exec(cmd: list[str], timeout: float, env: dict[str, str] | None = None) -> None:
    run_process(cmd, timeout=timeout, env=git_env(env))


def run_gitleaks(repo: Path, *, history: bool, timeout: float) -> list[SecretMatch]:
    """Spustí gitleaks a výsledky okamžitě maskuje. Report jde do soukromého dočasného souboru."""
    with tempfile.TemporaryDirectory(prefix="repo-doctor-gl-") as tmp:
        report = Path(tmp) / "report.json"
        cmd = [
            gitleaks_path() or "gitleaks",
            "detect",
            "--source",
            str(repo),
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--no-banner",
            "--exit-code",
            "0",
            "--log-level",
            "error",
        ]
        if not history:
            cmd.append("--no-git")
        # gitleaks volá `git log -p` sám – přebijeme filtry/textconv z konfigurace repa
        _exec(cmd, timeout, Git(repo).hardening_env())
        try:
            data = json.loads(report.read_text("utf-8") or "[]")
        except (OSError, json.JSONDecodeError):
            return []
    matches = []
    for item in data if isinstance(data, list) else []:
        secret = str(item.get("Secret") or item.get("Match") or "")
        date = str(item.get("Date") or "")
        ts: int | None = None
        if date:
            from datetime import datetime

            try:
                ts = int(datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = None
        file = str(item.get("File", ""))
        if file.startswith(str(repo)):
            file = file[len(str(repo)) :].lstrip("/")
        matches.append(
            SecretMatch(
                rule=str(item.get("RuleID", "gitleaks")),
                kind=str(item.get("Description") or item.get("RuleID") or _("tajemství")),
                masked=mask(secret),
                fingerprint=fingerprint(secret),
                path=file,
                line=int(item.get("StartLine") or 0),
                commit=str(item.get("Commit")) if item.get("Commit") else None,
                commit_ts=ts,
            )
        )
        del secret
    return matches
