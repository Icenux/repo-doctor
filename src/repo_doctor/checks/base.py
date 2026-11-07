"""Společné rozhraní kontrol a kontext repozitáře."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from repo_doctor.config import Config
from repo_doctor.discovery import matches_any
from repo_doctor.ecosystem import Ecosystem, detect
from repo_doctor.gitwrap import Git, StatusSummary
from repo_doctor.i18n import _
from repo_doctor.models import Category, Finding, Location, Patch, RemoteInfo, Severity, Visibility

if TYPE_CHECKING:
    from repo_doctor.deps import DepsData
    from repo_doctor.forges.base import ForgeSnapshot


class SkipCheck(Exception):
    """Kontrolu nelze provést (offline, bez hostingu…). Zpráva je důvod pro uživatele."""


@dataclass
class RepoContext:
    """Vše, co kontrola potřebuje vědět o repozitáři. Líné, cachované, jen čtení."""

    path: Path
    name: str
    root: str
    config: Config
    git: Git
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    offline: bool = False
    remotes: list[RemoteInfo] = field(default_factory=list)
    forge_name: str | None = None
    forge_snapshot: ForgeSnapshot | None = None
    forge_status: str | None = None  # důvod, proč data hostingu chybí
    visibility: Visibility = "unknown"
    deps: DepsData | None = None
    deps_status: str | None = None
    noreply_email: str | None = None  # noreply adresa uživatele na hostingu (z API)
    action_shas: dict[str, str] = field(default_factory=dict)  # "owner/repo@ref" → commit SHA
    # otisky vymyšlených hodnot od gitleaksu, které se nehlásí (počítají se v reportu)
    hidden_fakes: set[str] = field(default_factory=set)

    # -- soubory --------------------------------------------------------
    @cached_property
    def all_files(self) -> list[str]:
        """Sledované soubory + nesledované neignorované (bez ignore_paths z konfigurace)."""
        files = self.git.ls_files(untracked=True)
        ignore = self.config.ignore_paths
        return [f for f in files if not (ignore and matches_any(f, ignore))]

    @cached_property
    def all_files_set(self) -> frozenset[str]:
        return frozenset(self.all_files)

    @cached_property
    def tracked_files(self) -> list[str]:
        files = self.git.ls_files()
        ignore = self.config.ignore_paths
        return [f for f in files if not (ignore and matches_any(f, ignore))]

    @cached_property
    def tracked_set(self) -> frozenset[str]:
        return frozenset(self.tracked_files)

    @cached_property
    def ecosystems(self) -> set[Ecosystem]:
        return detect(self.all_files)

    @cached_property
    def status(self) -> StatusSummary:
        return self.git.status()

    @cached_property
    def default_branch(self) -> str | None:
        return self.git.default_branch()

    @cached_property
    def head(self) -> str | None:
        return self.git.head_sha()

    def exists(self, rel: str) -> bool:
        return (self.path / rel).exists()

    def has_any(self, *names: str) -> bool:
        lowered = {f.lower() for f in self.all_files}
        return any(n.lower() in lowered for n in names) or any(self.exists(n) for n in names)

    def read_text(self, rel: str, limit_kb: int | None = None) -> str | None:
        """Obsah souboru z pracovního stromu (None pro binární, příliš velké nebo chybějící)."""
        target = self.path / rel
        limit = (limit_kb or self.config.limits.max_file_kb) * 1024
        try:
            if not target.is_file() or target.is_symlink() or target.stat().st_size > limit:
                return None
            data = target.read_bytes()
        except OSError:
            return None
        if b"\0" in data[:8192]:
            return None
        return data.decode("utf-8", "replace")


class Check:
    """Základ kontroly. Podtřída nastaví metadata a implementuje `run`."""

    id: ClassVar[str]
    title: ClassVar[str]
    severity: ClassVar[Severity]
    category: ClassVar[Category]
    fixable: ClassVar[bool] = False
    network: ClassVar[bool] = False  # vyžaduje síť (v offline režimu přeskočeno)
    needs_forge: ClassVar[bool] = False  # vyžaduje data připojeného hostingu

    def run(self, repo: RepoContext) -> list[Finding]:
        raise NotImplementedError

    def fix(self, repo: RepoContext, findings: list[Finding]) -> Patch | None:
        """Navrhne opravu. Nesmí nic měnit – jen vrací Patch."""
        return None

    def fixes(self, repo: RepoContext, findings: list[Finding]) -> list[Patch]:
        """Všechny varianty opravy (výchozí: jediná z `fix`). Uživatel vybere nejvýš jednu."""
        patch = self.fix(repo, findings)
        return [patch] if patch is not None else []

    def finding(
        self,
        message: str,
        *,
        title: str | None = None,
        severity: Severity | None = None,
        path: str | None = None,
        line: int | None = None,
        commit: str | None = None,
        commit_date: datetime | None = None,
        snippet: str | None = None,
        kind: str | None = None,
        key: str = "",
        fixable: bool | None = None,
        **data: Any,
    ) -> Finding:
        return Finding(
            check_id=self.id,
            severity=severity or self.severity,
            category=self.category,
            title=title or _(self.title),
            message=message,
            location=Location(path=path, line=line, commit=commit, commit_date=commit_date),
            snippet=snippet,
            kind=kind,
            fixable=self.fixable if fixable is None else fixable,
            key=key,
            data=data,
        )

    def incomplete(self, what: str) -> Finding:
        """Nález „nekompletní sken“ – kontrola narazila na časový limit (obří repo)."""
        return Finding(
            check_id=INCOMPLETE_ID,
            severity=Severity.LOW,
            category=Category.MAINTENANCE,
            title=_("Nekompletní sken"),
            message=f"{self.id}: {what}",
            key=self.id,
        )


INCOMPLETE_ID = "scan-incomplete"
UNSAFE_OWNERSHIP_ID = "unsafe-ownership"


REGISTRY: dict[str, type[Check]] = {}


def register[C: type[Check]](cls: C) -> C:
    if cls.id in REGISTRY and REGISTRY[cls.id] is not cls:
        raise ValueError(_("duplicitní id kontroly: {id}").format(id=cls.id))
    REGISTRY[cls.id] = cls
    return cls


def ensure_loaded() -> None:
    import importlib
    import pkgutil

    import repo_doctor.checks as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if not mod.name.startswith("_") and mod.name != "base":
            importlib.import_module(f"{pkg.__name__}.{mod.name}")


def all_checks() -> list[Check]:
    ensure_loaded()
    order = {c: i for i, c in enumerate(Category)}
    return [cls() for cls in sorted(REGISTRY.values(), key=lambda c: (order[c.category], c.id))]


def get_check(check_id: str) -> Check | None:
    ensure_loaded()
    cls = REGISTRY.get(check_id)
    return cls() if cls else None


def check_ids() -> list[str]:
    return [c.id for c in all_checks()]


def select_checks(
    only: list[str] | None = None, skip: list[str] | None = None, disabled: list[str] | None = None
) -> list[Check]:
    checks = all_checks()
    known = {c.id for c in checks}
    for name in [*(only or []), *(skip or [])]:
        if name not in known:
            raise KeyError(name)
    if only:
        checks = [c for c in checks if c.id in only]
    excluded = set(skip or []) | set(disabled or [])
    return [c for c in checks if c.id not in excluded]


Predicate = Callable[[str], bool]
