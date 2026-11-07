"""JSON report se stabilním, verzovaným schématem (`schema_version`)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from repo_doctor import __version__
from repo_doctor.models import Finding, RepoDetails, RepoResult, ScanResult, Severity
from repo_doctor.scoring import band

SCHEMA_VERSION = 2  # 2: `repos[].band` je podle nejzávažnějšího nálezu (#73)


class JLocation(BaseModel):
    path: str | None = None
    line: int | None = None
    commit: str | None = None
    commit_date: datetime | None = None


class JFinding(BaseModel):
    id: str = Field(description="Check ID, e.g. secrets-history")
    fingerprint: str = Field(description="Stable finding hash for the allowlist")
    severity: Severity
    category: str
    title: str
    message: str
    location: JLocation
    snippet: str | None = Field(default=None, description="Always-masked sample (AKIA…(20 chars))")
    kind: str | None = None
    fixable: bool
    data: dict[str, str | int | list[str]] = Field(
        default_factory=dict,
        description="Check details (e.g. missing items, vulnerability IDs)",
    )


class JState(BaseModel):
    branch: str | None
    detached: bool
    default_branch: str | None
    uncommitted: int
    unpushed: int
    last_commit: datetime | None
    pulse_30d: list[int]


class JRepo(BaseModel):
    name: str
    path: str
    root: str
    score: int | None  # null = repo nešlo zkontrolovat
    untrusted_owner: bool
    band: str
    forge: str | None
    visibility: str
    archived: bool
    web_url: str | None
    remotes: list[dict[str, str | None]]
    state: JState
    counts: dict[str, int]
    findings: list[JFinding]
    allowlisted: int
    hidden_fakes: int = Field(
        default=0,
        description="Obviously made-up secret values reported by gitleaks and hidden (config hide_fake_secrets)",
    )
    skipped: dict[str, str]
    errors: dict[str, str]
    details: RepoDetails = Field(
        default_factory=RepoDetails,
        description="Languages, size, dependencies, history and hosting data (added in 0.4)",
    )


class JSummary(BaseModel):
    repos: int
    findings: dict[str, int]
    health: int
    no_pulse: int


class JReport(BaseModel):
    schema_version: int = SCHEMA_VERSION
    tool: dict[str, str]
    generated_at: datetime | None
    started_at: datetime
    offline: bool
    cancelled: bool
    roots: list[str]
    summary: JSummary
    repos: list[JRepo]
    warnings: list[str]


def _finding(f: Finding, repo: RepoResult) -> JFinding:
    return JFinding(
        id=f.check_id,
        fingerprint=f.fingerprint(repo.name),
        severity=f.severity,
        category=f.category.value,
        title=f.title,
        message=f.message,
        location=JLocation(**f.location.model_dump()),
        snippet=f.snippet,
        kind=f.kind,
        fixable=f.fixable,
        data=dict(f.data),
    )


def build(result: ScanResult, *, no_pulse_days: int = 90, now: datetime | None = None) -> JReport:
    summary = result.summary(no_pulse_days, now)
    repos = []
    for r in result.repos:
        repos.append(
            JRepo(
                name=r.name,
                path=r.path,
                root=r.root,
                score=r.score,
                untrusted_owner=r.untrusted_owner,
                band=band(r).value,
                forge=r.forge,
                visibility=r.visibility,
                archived=r.archived,
                web_url=r.web_url,
                remotes=[{"name": rm.name, "url": rm.url, "forge": rm.forge} for rm in r.remotes],
                state=JState(
                    branch=r.state.branch,
                    detached=r.state.detached,
                    default_branch=r.state.default_branch,
                    uncommitted=r.state.uncommitted,
                    unpushed=r.state.unpushed,
                    last_commit=r.state.last_commit,
                    pulse_30d=r.state.pulse,
                ),
                counts={s.value: n for s, n in r.counts().items()},
                findings=[
                    _finding(f, r)
                    for f in sorted(r.findings, key=lambda f: (-f.severity.rank, f.check_id))
                ],
                allowlisted=r.allowlisted,
                hidden_fakes=r.hidden_fakes,
                skipped=r.skipped,
                errors=r.errors,
                details=r.details,
            )
        )
    return JReport(
        tool={"name": "repo-doctor", "version": result.tool_version or __version__},
        generated_at=result.finished_at,
        started_at=result.started_at,
        offline=result.offline,
        cancelled=result.cancelled,
        roots=result.roots,
        summary=JSummary(
            repos=summary.repos,
            findings={s.value: n for s, n in summary.by_severity.items()},
            health=summary.health,
            no_pulse=summary.no_pulse,
        ),
        repos=repos,
        warnings=result.warnings,
    )


def render(result: ScanResult, *, no_pulse_days: int = 90, now: datetime | None = None) -> str:
    text = build(result, no_pulse_days=no_pulse_days, now=now).model_dump_json(indent=2)
    # tep (30 čísel) na jeden řádek – jinak by tvořil většinu souboru
    text = _PULSE.sub(
        lambda m: m.group(1) + "[" + ", ".join(m.group(2).split()).replace(",,", ",") + "]", text
    )
    return text + "\n"


_PULSE = re.compile(r'("pulse_30d": )\[([\s\d,]*)\]')


def json_schema() -> dict[str, Any]:
    schema = JReport.model_json_schema()
    schema["$id"] = f"https://github.com/Luquas95/repo-doctor/report-schema-v{SCHEMA_VERSION}.json"
    return schema


def schema_text() -> str:
    return json.dumps(json_schema(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":  # pragma: no cover
    print(schema_text(), end="")
