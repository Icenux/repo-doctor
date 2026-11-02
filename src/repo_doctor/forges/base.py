"""Společné rozhraní git hostingů.

Čtecí operace slouží skenu. Zápisové (`create_pr`, `merge_pr`) volá jen odeslání oprav
(`repo_doctor.publish`), vždy až po náhledu a potvrzení; neposílají se přes cache.
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from repo_doctor.config import ForgeConfig
from repo_doctor.httpclient import ForbiddenError, HttpClient, HttpError, NotFoundError
from repo_doctor.i18n import _

CIState = Literal["success", "failure", "pending", "none", "unknown"]


@dataclass
class ForgeRepo:
    full_name: str  # owner/repo (u GitLabu i s podskupinami)
    default_branch: str | None
    private: bool
    archived: bool = False
    html_url: str = ""
    clone_ssh: str = ""
    clone_https: str = ""
    pushed_at: datetime | None = None
    fork: bool = False
    description: str = ""
    id: str = ""  # interní ID (GitLab)
    stars: int | None = None
    forks: int | None = None
    open_issues: int | None = None  # u GitHubu včetně PR – scanner je odečte
    topics: tuple[str, ...] = ()
    created_at: datetime | None = None
    merge_methods: tuple[str, ...] = ()  # povolené metody merge (merge, squash, rebase)

    @property
    def name(self) -> str:
        return self.full_name.rsplit("/", 1)[-1]

    @property
    def visibility(self) -> Literal["public", "private"]:
        return "private" if self.private else "public"


@dataclass
class CICheck:
    """Jedna kontrola CI (job, check run nebo commit status) – jen pro zobrazení."""

    name: str
    state: CIState
    url: str = ""


@dataclass
class CIStatus:
    state: CIState
    ref: str = ""
    number: str = ""
    url: str = ""
    checks: list[CICheck] = field(default_factory=list)  # vyplňuje jen `pr_ci`


@dataclass
class PullRequest:
    number: int
    title: str
    updated_at: datetime | None
    kind: Literal["pr", "issue"] = "pr"


PRState = Literal["open", "merged", "closed"]


@dataclass
class PullRequestInfo:
    """Pull/merge request vytvořený z opravné větve (stav se čte vždy čerstvě)."""

    number: int
    url: str
    state: PRState
    title: str = ""
    head_branch: str = ""
    base_branch: str = ""
    head_sha: str = ""
    mergeable: bool | None = None  # None = hosting to ještě počítá


@dataclass
class ConnectionReport:
    ok: bool
    user: str | None = None
    repo_count: int | None = None
    scopes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ForgeSnapshot:
    """Data z hostingu o jednom repozitáři (předem načtená, kontroly už síť nevolají)."""

    forge: str
    kind: str
    owner_path: str
    repo: ForgeRepo | None = None
    missing_reason: str | None = None
    default_branch_sha: str | None = None
    ci: CIStatus | None = None
    protected: bool | None = None
    alerts: dict[str, int] | None = None
    open_items: list[PullRequest] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_OWNER_PATH = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+")


def valid_owner_path(owner_path: str) -> bool:
    """`owner/repo` (GitLab i s podskupinami). Brání posílání dotazů na libovolné cesty API."""
    return bool(_OWNER_PATH.fullmatch(owner_path)) and not any(
        part in {".", ".."} for part in owner_path.split("/")
    )


def opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def sub_dict(value: object, key: str) -> dict[str, Any]:
    """Vnořený objekt z JSON odpovědi (`{}`, pokud chybí nebo není objekt)."""
    inner = value.get(key) if isinstance(value, dict) else None
    return inner if isinstance(inner, dict) else {}


def int_field(value: object, key: str) -> int:
    got = value.get(key) if isinstance(value, dict) else None
    return got if isinstance(got, int) and not isinstance(got, bool) else 0


def str_tuple(value: object) -> tuple[str, ...]:
    return tuple(str(v) for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Forge(abc.ABC):
    """Základ implementace hostingu. `client` má nastavenou base URL API a autorizaci."""

    kind: str = ""
    authenticated: bool = True  # False = bez tokenu (zápis nejde)

    def __init__(self, config: ForgeConfig, client: HttpClient) -> None:
        self.config = config
        self.client = client

    @property
    def name(self) -> str:
        return self.config.name

    @abc.abstractmethod
    async def list_repos(self) -> list[ForgeRepo]: ...

    @abc.abstractmethod
    async def get_repo(self, owner_path: str) -> ForgeRepo: ...

    async def default_branch(self, owner_path: str) -> str | None:
        return (await self.get_repo(owner_path)).default_branch

    @abc.abstractmethod
    async def branch_head(self, repo: ForgeRepo, branch: str) -> tuple[str | None, bool | None]:
        """(SHA posledního commitu, je chráněná?)"""

    @abc.abstractmethod
    async def latest_ci_status(self, repo: ForgeRepo, branch: str, sha: str | None) -> CIStatus: ...

    async def branch_protection(self, repo: ForgeRepo, branch: str) -> bool | None:
        return (await self.branch_head(repo, branch))[1]

    async def security_alerts(self, repo: ForgeRepo) -> dict[str, int] | None:
        return None  # hosting je neumí zjistit

    @abc.abstractmethod
    async def open_prs(self, repo: ForgeRepo) -> list[PullRequest]: ...

    @abc.abstractmethod
    async def find_pr(self, repo: ForgeRepo, head: str) -> PullRequestInfo | None:
        """Nejnovější PR/MR z větve `head` (otevřený, mergnutý i zavřený), jinak None."""

    @abc.abstractmethod
    async def get_pr(self, repo: ForgeRepo, number: int) -> PullRequestInfo: ...

    @abc.abstractmethod
    async def create_pr(
        self, repo: ForgeRepo, *, head: str, base: str, title: str, body: str
    ) -> PullRequestInfo: ...

    @abc.abstractmethod
    async def pr_ci(self, repo: ForgeRepo, pr: PullRequestInfo) -> CIStatus:
        """Stav CI pro poslední commit PR (ne pro výchozí větev)."""

    @abc.abstractmethod
    async def merge_pr(self, repo: ForgeRepo, pr: PullRequestInfo, method: str | None) -> None:
        """Mergne PR; `method` je merge/squash/rebase, None = výchozí metoda hostingu.

        Hosting dostane SHA hlavičky PR, takže merge selže, pokud se větev mezitím změnila.
        """

    @abc.abstractmethod
    async def test_connection(self) -> ConnectionReport: ...

    async def noreply_email(self) -> str | None:
        """`noreply` adresa přihlášeného uživatele pro commity (cachovaná), nebo None."""
        if not hasattr(self, "_noreply"):
            try:
                self._noreply: str | None = await self._fetch_noreply()
            except HttpError:
                self._noreply = None
        return self._noreply

    async def _fetch_noreply(self) -> str | None:
        return None

    async def snapshot(self, owner_path: str) -> ForgeSnapshot:
        """Načte vše potřebné pro kontroly hostingu. Chyby jednotlivých částí jen zaznamená."""
        snap = ForgeSnapshot(forge=self.name, kind=self.kind, owner_path=owner_path)
        if not valid_owner_path(owner_path):
            snap.missing_reason = _("neplatná cesta repa v URL remote")
            return snap
        try:
            snap.repo = await self.get_repo(owner_path)
        except NotFoundError:
            snap.missing_reason = _("hosting repo nezná (smazané, přejmenované nebo bez přístupu)")
            return snap
        except ForbiddenError:
            snap.missing_reason = _("k repu nemá token přístup")
            return snap
        repo = snap.repo
        branch = repo.default_branch
        if branch:
            try:
                snap.default_branch_sha, snap.protected = await self.branch_head(repo, branch)
            except HttpError as err:
                snap.errors.append(f"branch: {err}")
            try:
                snap.ci = await self.latest_ci_status(repo, branch, snap.default_branch_sha)
            except HttpError as err:
                snap.errors.append(f"CI: {err}")
        try:
            snap.alerts = await self.security_alerts(repo)
        except HttpError as err:
            snap.errors.append(f"alerty: {err}")
        try:
            snap.open_items = await self.open_prs(repo)
        except HttpError as err:
            snap.errors.append(f"PR: {err}")
        return snap


MERGE_PREFERENCE = ("merge", "squash", "rebase")


def pick_merge_method(repo: ForgeRepo, wanted: str) -> str | None:
    """Metoda merge podle nastavení (`auto` = první povolená v pořadí merge → squash → rebase).

    None znamená „neposílej metodu“ (hosting použije vlastní výchozí). Výslovně zvolenou,
    ale v repu zakázanou metodu vrací beze změny – hosting ji odmítne srozumitelnou chybou.
    """
    if wanted != "auto":
        return wanted
    for method in MERGE_PREFERENCE:
        if method in repo.merge_methods:
            return method
    return None


def is_stale(item: PullRequest, days: int, now: datetime) -> bool:
    return item.updated_at is not None and now - item.updated_at > timedelta(days=days)
