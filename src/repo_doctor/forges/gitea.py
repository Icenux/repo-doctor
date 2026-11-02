"""Gitea a Forgejo – společná implementace nad kompatibilním API /api/v1."""

from __future__ import annotations

from urllib.parse import quote

from repo_doctor.config import ForgeConfig
from repo_doctor.forges.base import (
    CICheck,
    CIStatus,
    ConnectionReport,
    Forge,
    ForgeRepo,
    PullRequest,
    PullRequestInfo,
    int_field,
    opt_int,
    parse_time,
    str_tuple,
    sub_dict,
)
from repo_doctor.httpclient import ForbiddenError, HttpClient, HttpError, NotFoundError
from repo_doctor.i18n import _


def api_base(url: str) -> str:
    return url.rstrip("/") + "/api/v1"


def auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"token {token}"} if token else {}


def _merge_methods(d: dict[str, object]) -> tuple[str, ...]:
    flags = (
        ("merge", ("allow_merge_commits",)),
        ("squash", ("allow_squash_merge",)),
        ("rebase", ("allow_rebase", "allow_rebase_explicit")),
    )
    return tuple(method for method, keys in flags if any(d.get(k) for k in keys))


def _pr(d: dict[str, object]) -> PullRequestInfo:
    head, base = sub_dict(d, "head"), sub_dict(d, "base")
    mergeable = d.get("mergeable")
    return PullRequestInfo(
        number=int_field(d, "number"),
        url=str(d.get("html_url") or ""),
        state="merged" if d.get("merged") else ("open" if d.get("state") == "open" else "closed"),
        title=str(d.get("title") or ""),
        head_branch=str(head.get("ref") or ""),
        base_branch=str(base.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        mergeable=mergeable if isinstance(mergeable, bool) else None,
    )


def _repo(d: dict[str, object]) -> ForgeRepo:
    return ForgeRepo(
        full_name=str(d.get("full_name", "")),
        default_branch=str(d["default_branch"]) if d.get("default_branch") else None,
        private=bool(d.get("private")) or bool(d.get("internal")),
        archived=bool(d.get("archived")),
        html_url=str(d.get("html_url") or ""),
        clone_ssh=str(d.get("ssh_url") or ""),
        clone_https=str(d.get("clone_url") or ""),
        pushed_at=parse_time(d.get("updated_at")),
        fork=bool(d.get("fork")),
        description=str(d.get("description") or ""),
        id=str(d.get("id") or ""),
        stars=opt_int(d.get("stars_count")),
        forks=opt_int(d.get("forks_count")),
        open_issues=opt_int(d.get("open_issues_count")),
        topics=str_tuple(d.get("topics")),
        created_at=parse_time(d.get("created_at")),
        merge_methods=_merge_methods(d),
    )


class GiteaForge(Forge):
    kind = "gitea"

    def __init__(
        self, config: ForgeConfig, client: HttpClient, *, authenticated: bool = True
    ) -> None:
        super().__init__(config, client)
        self.authenticated = authenticated
        self.kind = config.type  # "gitea" nebo "forgejo"

    async def list_repos(self) -> list[ForgeRepo]:
        if self.config.org:
            url = f"/orgs/{quote(self.config.org)}/repos"
        elif self.authenticated:
            url = "/user/repos"
        elif self.config.user:
            url = f"/users/{quote(self.config.user)}/repos"
        else:
            return []
        return [_repo(d) for d in await self.client.paginate(url, params={"limit": 50})]

    async def get_repo(self, owner_path: str) -> ForgeRepo:
        return _repo(await self.client.get_json(f"/repos/{owner_path}"))

    async def branch_head(self, repo: ForgeRepo, branch: str) -> tuple[str | None, bool | None]:
        data = await self.client.get_json(
            f"/repos/{repo.full_name}/branches/{quote(branch, safe='')}"
        )
        commit = data.get("commit") or {}
        protected = data.get("protected")
        return commit.get("id") or commit.get("sha"), bool(
            protected
        ) if protected is not None else None

    async def latest_ci_status(
        self,
        repo: ForgeRepo,
        branch: str,
        sha: str | None,
        *,
        ttl: float | None = None,
        with_checks: bool = False,
    ) -> CIStatus:
        """Gitea/Forgejo Actions hlásí výsledek jako commit status výchozí branche."""
        if not sha:
            return CIStatus("unknown", branch)
        try:
            data = await self.client.get_json(
                f"/repos/{repo.full_name}/commits/{sha}/status", ttl=ttl
            )
        except (NotFoundError, ForbiddenError):
            return CIStatus("unknown", branch)
        if not data.get("total_count") and not data.get("statuses"):
            return CIStatus("none", branch)
        state = str(data.get("state") or "")
        mapped = {
            "success": "success",
            "failure": "failure",
            "error": "failure",
            "pending": "pending",
            "warning": "success",
        }
        statuses = data.get("statuses") or [{}]
        url = str(statuses[0].get("target_url") or "")
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        number = tail if "/runs/" in url and tail.isdigit() else ""
        checks = (
            [
                CICheck(
                    str(st.get("context") or "?"),
                    mapped.get(str(st.get("status") or st.get("state")), "pending"),  # type: ignore[arg-type]
                    str(st.get("target_url") or ""),
                )
                for st in data.get("statuses") or []
            ]
            if with_checks
            else []
        )
        return CIStatus(mapped.get(state, "none"), branch, number, url, checks)  # type: ignore[arg-type]

    async def open_prs(self, repo: ForgeRepo) -> list[PullRequest]:
        items = await self.client.paginate(
            f"/repos/{repo.full_name}/issues",
            params={"state": "open", "type": "all", "limit": 50},
            max_pages=3,
        )
        return [
            PullRequest(
                number=int(i.get("number", 0)),
                title=str(i.get("title", "")),
                updated_at=parse_time(i.get("updated_at")),
                kind="pr" if "pull_request" in i else "issue",
            )
            for i in items
        ]

    async def find_pr(self, repo: ForgeRepo, head: str) -> PullRequestInfo | None:
        # API nemá filtr podle větve – projdeme nejnovější stránky a vybereme nejvyšší číslo
        items = await self.client.paginate(
            f"/repos/{repo.full_name}/pulls",
            params={"state": "all", "limit": 50},
            max_pages=3,
            ttl=0,
        )
        found = [_pr(i) for i in items if (i.get("head") or {}).get("ref") == head]
        return max(found, key=lambda pr: pr.number) if found else None

    async def get_pr(self, repo: ForgeRepo, number: int) -> PullRequestInfo:
        return _pr(await self.client.get_json(f"/repos/{repo.full_name}/pulls/{number}", ttl=0))

    async def create_pr(
        self, repo: ForgeRepo, *, head: str, base: str, title: str, body: str
    ) -> PullRequestInfo:
        data = await self.client.post_json(
            f"/repos/{repo.full_name}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
            ttl=0,
        )
        return _pr(data)

    async def pr_ci(self, repo: ForgeRepo, pr: PullRequestInfo) -> CIStatus:
        return await self.latest_ci_status(
            repo, pr.head_branch, pr.head_sha or None, ttl=0, with_checks=True
        )

    async def merge_pr(self, repo: ForgeRepo, pr: PullRequestInfo, method: str | None) -> None:
        await self.client.request_json(
            "POST",
            f"/repos/{repo.full_name}/pulls/{pr.number}/merge",
            json_body={"Do": method or "merge", "head_commit_id": pr.head_sha},
            ttl=0,
        )

    async def _fetch_noreply(self) -> str | None:
        if not self.authenticated:
            return None
        user = await self.client.get_json("/user")
        value = str(user.get("email") or "")
        # se skrytým e-mailem vrací Gitea/Forgejo adresu typu `login@noreply.<instance>`
        return value if "noreply" in value.split("@", 1)[-1] else None

    async def test_connection(self) -> ConnectionReport:
        warnings: list[str] = []
        try:
            if self.authenticated:
                user = await self.client.get_json("/user")
            elif self.config.user:
                user = await self.client.get_json(f"/users/{quote(self.config.user)}")
                warnings.append(_("bez tokenu: jen veřejná repa"))
            else:
                return ConnectionReport(
                    False, error=_("bez tokenu je potřeba vyplnit uživatele nebo organizaci")
                )
            login = str(user.get("login") or user.get("username") or "")
            repos = await self.list_repos()
            try:
                version = await self.client.get_json("/version", ttl=0)
                detected = (
                    "forgejo" if "forgejo" in str(version.get("version", "")).lower() else None
                )
                if detected and self.config.type == "gitea":
                    warnings.append(_('instance je Forgejo – nastav type = "forgejo"'))
            except HttpError:
                pass
        except HttpError as err:
            return ConnectionReport(False, error=str(err))
        if self.authenticated:
            warnings.append(
                _(
                    "rozsah tokenu Gitea/Forgejo API neprozradí – pro sken stačí read:repository, read:user "
                    "(a read:issue pro PR); PR a merge z odeslání oprav potřebují write:repository "
                    "a write:issue"
                )
            )
        if not self.config.verify_tls and not self.config.ca_bundle:
            warnings.append(
                _("ověřování TLS je vypnuté (verify_tls = false) – raději nastav ca_bundle")
            )
        return ConnectionReport(True, user=login, repo_count=len(repos), warnings=warnings)
