"""Datové modely jádra (pydantic v2). Nezávislé na UI."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repo_doctor.i18n import _


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]

    @property
    def symbol(self) -> str:
        return {"high": "▲", "medium": "◆", "low": "●"}[self.value]

    @property
    def short(self) -> str:
        return {"high": "HIGH", "medium": "MED", "low": "LOW"}[self.value]

    @classmethod
    def parse(cls, value: str) -> Severity:
        aliases = {"med": "medium", "hi": "high", "lo": "low"}
        return cls(aliases.get(value.lower(), value.lower()))


class Category(StrEnum):
    SECURITY = "security"
    MAINTENANCE = "maintenance"
    GIT = "git"
    FORGE = "forge"

    @property
    def label(self) -> str:
        return {
            "security": _("Bezpečnost"),
            "maintenance": _("Údržba"),
            "git": _("Stav gitu"),
            "forge": _("Hosting"),
        }[self.value]


class Location(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str | None = None
    line: int | None = None
    commit: str | None = None
    commit_date: datetime | None = None

    def render(self) -> str:
        parts: list[str] = []
        if self.path:
            parts.append(f"{self.path}:{self.line}" if self.line else self.path)
        if self.commit:
            parts.append(f"commit {self.commit[:7]}")
        if self.commit_date:
            parts.append(self.commit_date.strftime("%-d. %-m. %Y"))
        return " · ".join(parts)


class Finding(BaseModel):
    """Jeden nález. `snippet` je vždy už maskovaný – surové tajemství model nikdy nenese."""

    model_config = ConfigDict(frozen=True)

    check_id: str
    severity: Severity
    category: Category
    title: str
    message: str
    location: Location = Field(default_factory=Location)
    snippet: str | None = None
    kind: str | None = None
    fixable: bool = False
    key: str = ""
    data: dict[str, str | int | list[str]] = Field(default_factory=dict)

    def fingerprint(self, repo_name: str) -> str:
        """Stabilní hash nálezu pro allowlist (nezávislý na čísle řádku)."""
        raw = "\0".join(
            [self.check_id, repo_name, self.location.path or "", self.key or self.message]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class RemoteInfo(BaseModel):
    name: str
    url: str  # redigovaná (bez přihlašovacích údajů)
    host: str | None = None
    owner_path: str | None = None
    forge: str | None = None  # název připojeného hostingu


class RepoState(BaseModel):
    branch: str | None = None
    detached: bool = False
    head: str | None = None
    default_branch: str | None = None
    uncommitted: int = 0
    unpushed: int = 0
    last_commit: datetime | None = None
    pulse: list[int] = Field(default_factory=list)  # commity za den, nejstarší první
    commit_count: int = 0
    root_commit: str | None = None  # nejstarší kořenový commit HEAD (duplicitní klony bez remote)


class LanguageShare(BaseModel):
    name: str
    bytes: int


class EcosystemInfo(BaseModel):
    name: str  # python, node, rust, go
    managers: list[str] = Field(default_factory=list)  # uv, npm, pnpm, cargo…
    direct: int = 0  # přímé závislosti
    total: int = 0  # všechny (z lockfilu)


class AuthorCount(BaseModel):
    name: str
    commits: int


class RepoDetails(BaseModel):
    """Popis repa pro kartu a reporty (offline z gitu; `forge_*` z API hostingu)."""

    languages: list[LanguageShare] = Field(default_factory=list)  # sestupně podle velikosti
    files: int | None = None
    worktree_bytes: int | None = None  # součet velikostí souborů v HEAD
    git_bytes: int | None = None  # objekty v .git
    ecosystems: list[EcosystemInfo] = Field(default_factory=list)
    license: str | None = None
    first_commit: datetime | None = None
    commits_30d: int | None = None
    commits_year: int | None = None
    authors: int | None = None
    top_authors: list[AuthorCount] = Field(default_factory=list)
    branches: int | None = None
    last_tag: str | None = None
    last_tag_date: datetime | None = None
    forge_description: str | None = None
    forge_topics: list[str] = Field(default_factory=list)
    forge_stars: int | None = None
    forge_forks: int | None = None
    forge_open_issues: int | None = None
    forge_created: datetime | None = None
    forge_pushed: datetime | None = None


Visibility = Literal["public", "private", "unknown"]


class RepoResult(BaseModel):
    path: str
    name: str
    root: str
    remotes: list[RemoteInfo] = Field(default_factory=list)
    forge: str | None = None
    visibility: Visibility = "unknown"
    web_url: str | None = None
    archived: bool = False
    state: RepoState = Field(default_factory=RepoState)
    findings: list[Finding] = Field(default_factory=list)
    allowlisted: int = 0
    hidden_fakes: int = 0  # zjevně vymyšlené hodnoty od gitleaksu, které se nehlásí
    skipped: dict[str, str] = Field(default_factory=dict)  # check_id → důvod
    errors: dict[str, str] = Field(default_factory=dict)
    forge_info: dict[str, str | int | bool | None] = Field(default_factory=dict)
    details: RepoDetails = Field(default_factory=RepoDetails)
    score: int | None = 100  # None = repo nešlo zkontrolovat (např. nedůvěryhodný vlastník)
    untrusted_owner: bool = False  # git odmítl repo kvůli safe.directory
    duration_ms: int = 0

    def counts(self) -> dict[Severity, int]:
        result = dict.fromkeys(Severity, 0)
        for f in self.findings:
            result[f.severity] += 1
        return result

    @property
    def worst(self) -> Severity | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: s.rank)


class ScanSummary(BaseModel):
    repos: int = 0
    by_severity: dict[Severity, int] = Field(default_factory=lambda: dict.fromkeys(Severity, 0))
    health: int = 100
    no_pulse: int = 0


class ScanResult(BaseModel):
    schema_version: int = 1
    tool_version: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    offline: bool = False
    roots: list[str] = Field(default_factory=list)
    repos: list[RepoResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cancelled: bool = False

    def summary(self, no_pulse_days: int = 90, now: datetime | None = None) -> ScanSummary:
        from repo_doctor.scoring import is_no_pulse

        by: dict[Severity, int] = dict.fromkeys(Severity, 0)
        for repo in self.repos:
            for sev, n in repo.counts().items():
                by[sev] += n
        scored = [r.score for r in self.repos if r.score is not None]
        health = round(sum(scored) / len(scored)) if scored else 100
        no_pulse = sum(1 for r in self.repos if is_no_pulse(r, no_pulse_days, now))
        return ScanSummary(repos=len(self.repos), by_severity=by, health=health, no_pulse=no_pulse)


class FileChange(BaseModel):
    """Jedna změna souboru. Operace se vyhodnocují až nad aktuálním obsahem (kvůli řetězení)."""

    path: str
    action: Literal["create", "append", "untrack", "replace"]
    content: str = ""
    header: str = ""  # komentář nad doplněným blokem (append)
    old: str | None = None  # obsah v HEAD – pro náhled diffu

    def result(self, current: str | None) -> str | None:
        """Nový obsah souboru; `None` = odstranit z indexu (soubor na disku zůstává)."""
        if self.action == "untrack":
            return None
        if self.action == "replace":
            # celý nový obsah – jen pokud soubor od návrhu nikdo nezměnil
            return self.content if current == self.old else current
        if self.action == "create":
            return self.content if current is None else current  # existující soubor nepřepisujeme
        existing = {line.strip() for line in (current or "").splitlines()}
        additions = [
            line
            for line in self.content.splitlines()
            if line.strip() and line.strip() not in existing
        ]
        if not additions:
            return current if current is not None else ""
        base = current or ""
        if base and not base.endswith("\n"):
            base += "\n"
        block = ([self.header] if self.header else []) + additions
        separator = "\n" if base.strip() else ""
        return base + separator + "\n".join(block) + "\n"


class LocalAction(BaseModel):
    """Změna jen v `.git/` repa uživatele (nic se necommituje), např. lokální `user.email`.

    `display_old`/`display_new` jsou podoby pro náhled (např. maskovaný e-mail).
    """

    kind: Literal["git_config", "hook"]
    key: str  # klíč konfigurace, nebo název hooku (`pre-push`)
    value: str  # nová hodnota, nebo obsah hooku
    description: str
    display_old: str | None = None
    display_new: str | None = None


class Patch(BaseModel):
    """Navržená oprava. Vygenerování patche nic nemění, aplikace je samostatný krok."""

    check_id: str
    title: str
    summary: str
    changes: list[FileChange] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    local: list[LocalAction] = Field(default_factory=list)
    variant: str = ""  # víc alternativ jedné kontroly (uživatel vybere jednu)

    @property
    def key(self) -> str:
        return f"{self.check_id}:{self.variant}" if self.variant else self.check_id

    @property
    def local_only(self) -> bool:
        return not self.changes and bool(self.local)

    @property
    def commit_message(self) -> str:
        return f"chore(repo-doctor): {self.check_id}\n\n{self.summary}\n"

    def diff(self) -> str:
        from repo_doctor.fixes import render_diff

        result: str = render_diff(self)
        return result
