"""GitLab (REST v4) – gitlab.com i self-hosted."""

from __future__ import annotations

from urllib.parse import quote

from repo_doctor.config import ForgeConfig
from repo_doctor.forges.base import (
    CICheck,
    CIState,
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
)
from repo_doctor.httpclient import ForbiddenError, HttpClient, HttpError, NotFoundError
from repo_doctor.i18n import _

BROAD_SCOPES = {
    "api",
    "write_repository",
    "sudo",
    "admin_mode",
    "write_registry",
    "create_runner",
    "k8s_proxy",
}


def api_base(url: str | None) -> str:
    return (url or "https://gitlab.com").rstrip("/") + "/api/v4"


def auth_headers(token: str | None) -> dict[str, str]:
    return {"PRIVATE-TOKEN": token} if token else {}


def _repo(d: dict[str, object]) -> ForgeRepo:
    return ForgeRepo(
        full_name=str(d.get("path_with_namespace", "")),
        default_branch=str(d["default_branch"]) if d.get("default_branch") else None,
        private=d.get("visibility") != "public",
        archived=bool(d.get("archived")),
        html_url=str(d.get("web_url") or ""),
        clone_ssh=str(d.get("ssh_url_to_repo") or ""),
        clone_https=str(d.get("http_url_to_repo") or ""),
        pushed_at=parse_time(d.get("last_activity_at")),
        fork="forked_from_project" in d,
        description=str(d.get("description") or ""),
        id=str(d.get("id") or ""),
        stars=opt_int(d.get("star_count")),
        forks=opt_int(d.get("forks_count")),
        open_issues=opt_int(d.get("open_issues_count")),
        topics=str_tuple(d.get("topics") or d.get("tag_list")),
        created_at=parse_time(d.get("created_at")),
    )


_RUNNING = frozenset(
    {"running", "pending", "created", "waiting_for_resource", "preparing", "scheduled"}
)


def _pipeline_state(status: str) -> CIState:
    if status == "failed":
        return "failure"
    if status == "success":
        return "success"
    return "pending" if status in _RUNNING else "none"


def _mr(d: dict[str, object]) -> PullRequestInfo:
    state = str(d.get("state") or "")
    detailed = str(d.get("detailed_merge_status") or "")
    legacy = str(d.get("merge_status") or "")
    mergeable: bool | None
    if detailed in ("mergeable", "ci_still_running", "ci_must_pass") or legacy == "can_be_merged":
        mergeable = True  # CI vyhodnocuje publish zvlášť
    elif detailed in ("checking", "unchecked", "preparing") or legacy in ("checking", "unchecked"):
        mergeable = None
    else:
        mergeable = False
    return PullRequestInfo(
        number=int_field(d, "iid"),
        url=str(d.get("web_url") or ""),
        state="merged" if state == "merged" else ("open" if state == "opened" else "closed"),
        title=str(d.get("title") or ""),
        head_branch=str(d.get("source_branch") or ""),
        base_branch=str(d.get("target_branch") or ""),
        head_sha=str(d.get("sha") or ""),
        mergeable=mergeable,
    )


def _pid(repo: ForgeRepo) -> str:
    return repo.id or quote(repo.full_name, safe="")


class GitLabForge(Forge):
    kind = "gitlab"

    def __init__(
        self, config: ForgeConfig, client: HttpClient, *, authenticated: bool = True
    ) -> None:
        super().__init__(config, client)
        self.authenticated = authenticated

    async def list_repos(self) -> list[ForgeRepo]:
        if self.config.org:
            url = f"/groups/{quote(self.config.org, safe='')}/projects"
            params: dict[str, str | int] = {"per_page": 100, "include_subgroups": "true"}
        elif self.authenticated:
            url, params = "/projects", {"per_page": 100, "membership": "true"}
        elif self.config.user:
            url, params = f"/users/{quote(self.config.user)}/projects", {"per_page": 100}
        else:
            return []
        return [_repo(d) for d in await self.client.paginate(url, params=params)]

    async def get_repo(self, owner_path: str) -> ForgeRepo:
        return _repo(await self.client.get_json(f"/projects/{quote(owner_path, safe='')}"))

    async def branch_head(self, repo: ForgeRepo, branch: str) -> tuple[str | None, bool | None]:
        data = await self.client.get_json(
            f"/projects/{_pid(repo)}/repository/branches/{quote(branch, safe='')}"
        )
        protected = data.get("protected")
        return (data.get("commit") or {}).get("id"), bool(
            protected
        ) if protected is not None else None

    async def latest_ci_status(self, repo: ForgeRepo, branch: str, sha: str | None) -> CIStatus:
        try:
            items = await self.client.get_json(
                f"/projects/{_pid(repo)}/pipelines",
                params={"ref": branch, "per_page": 1, "order_by": "id", "sort": "desc"},
            )
        except (NotFoundError, ForbiddenError):
            return CIStatus("unknown", branch)
        if not items:
            return CIStatus("none", branch)
        p = items[0]
        return CIStatus(
            _pipeline_state(str(p.get("status", ""))),
            branch,
            str(p.get("iid") or p.get("id") or ""),
            str(p.get("web_url") or ""),
        )

    async def open_prs(self, repo: ForgeRepo) -> list[PullRequest]:
        result: list[PullRequest] = []
        for path, kind in (("merge_requests", "pr"), ("issues", "issue")):
            items = await self.client.paginate(
                f"/projects/{_pid(repo)}/{path}",
                params={"state": "opened", "per_page": 100},
                max_pages=3,
            )
            result.extend(
                PullRequest(
                    number=int(i.get("iid", 0)),
                    title=str(i.get("title", "")),
                    updated_at=parse_time(i.get("updated_at")),
                    kind=kind,  # type: ignore[arg-type]
                )
                for i in items
            )
        return result

    async def find_pr(self, repo: ForgeRepo, head: str) -> PullRequestInfo | None:
        items = await self.client.get_json(
            f"/projects/{_pid(repo)}/merge_requests",
            params={
                "source_branch": head,
                "state": "all",
                "order_by": "created_at",
                "sort": "desc",
                "per_page": 1,
            },
            ttl=0,
        )
        return _mr(items[0]) if isinstance(items, list) and items else None

    async def get_pr(self, repo: ForgeRepo, number: int) -> PullRequestInfo:
        return _mr(
            await self.client.get_json(f"/projects/{_pid(repo)}/merge_requests/{number}", ttl=0)
        )

    async def create_pr(
        self, repo: ForgeRepo, *, head: str, base: str, title: str, body: str
    ) -> PullRequestInfo:
        data = await self.client.post_json(
            f"/projects/{_pid(repo)}/merge_requests",
            {
                "source_branch": head,
                "target_branch": base,
                "title": title,
                "description": body,
                "remove_source_branch": False,
            },
            ttl=0,
        )
        return _mr(data)

    async def pr_ci(self, repo: ForgeRepo, pr: PullRequestInfo) -> CIStatus:
        if not pr.head_sha:
            return CIStatus("unknown", pr.head_branch)
        try:
            items = await self.client.get_json(
                f"/projects/{_pid(repo)}/pipelines",
                params={"sha": pr.head_sha, "per_page": 1, "order_by": "id", "sort": "desc"},
                ttl=0,
            )
        except (NotFoundError, ForbiddenError):
            return CIStatus("unknown", pr.head_branch)
        if not items:
            return CIStatus("none", pr.head_branch)
        p = items[0]
        checks: list[CICheck] = []
        try:
            jobs = await self.client.get_json(
                f"/projects/{_pid(repo)}/pipelines/{p.get('id')}/jobs",
                params={"per_page": 100},
                ttl=0,
            )
        except (NotFoundError, ForbiddenError):
            jobs = []
        for job in jobs if isinstance(jobs, list) else []:
            status = str(job.get("status", ""))
            state = (
                "success" if status in ("success", "skipped", "manual") else _pipeline_state(status)
            )
            if status == "canceled":
                state = "failure"
            checks.append(
                CICheck(str(job.get("name") or "?"), state, str(job.get("web_url") or ""))
            )
        return CIStatus(
            _pipeline_state(str(p.get("status", ""))),
            pr.head_branch,
            str(p.get("iid") or p.get("id") or ""),
            str(p.get("web_url") or ""),
            checks,
        )

    async def merge_pr(self, repo: ForgeRepo, pr: PullRequestInfo, method: str | None) -> None:
        # metodu merge (merge commit / rebase / ff) má GitLab v nastavení projektu, volit jde jen squash
        await self.client.request_json(
            "PUT",
            f"/projects/{_pid(repo)}/merge_requests/{pr.number}/merge",
            json_body={"sha": pr.head_sha, "squash": method == "squash"},
            ttl=0,
        )

    async def _fetch_noreply(self) -> str | None:
        if not self.authenticated:
            return None
        user = await self.client.get_json("/user")
        for key in ("private_commit_email", "commit_email"):
            value = str(user.get(key) or "")
            if "noreply" in value:
                return value
        return None

    async def test_connection(self) -> ConnectionReport:
        warnings: list[str] = []
        scopes: list[str] = []
        try:
            if self.authenticated:
                user = await self.client.get_json("/user")
                try:
                    token_info = await self.client.get_json("/personal_access_tokens/self", ttl=0)
                    scopes = [str(s) for s in token_info.get("scopes", [])]
                except HttpError:
                    warnings.append(
                        _("rozsah tokenu nejde ověřit (starší GitLab nebo jiný typ tokenu)")
                    )
            elif self.config.user:
                users = await self.client.get_json("/users", params={"username": self.config.user})
                user = users[0] if users else {}
                warnings.append(_("bez tokenu: jen veřejné projekty"))
            else:
                return ConnectionReport(
                    False, error=_("bez tokenu je potřeba vyplnit uživatele nebo skupinu")
                )
            repos = await self.list_repos()
        except HttpError as err:
            return ConnectionReport(False, error=str(err))
        broad = sorted(set(scopes) & BROAD_SCOPES)
        if broad:
            warnings.append(
                _(
                    "token má zbytečně široká práva ({scopes}); pro sken stačí read_api "
                    "(PR a merge z odeslání oprav potřebují api)"
                ).format(scopes=", ".join(broad))
            )
        return ConnectionReport(
            True,
            user=str(user.get("username", "")),
            repo_count=len(repos),
            scopes=scopes,
            warnings=warnings,
        )
