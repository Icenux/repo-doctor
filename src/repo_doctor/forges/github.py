"""GitHub (REST v3), včetně GitHub Enterprise přes vlastní URL."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

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
    sub_dict,
    valid_owner_path,
)
from repo_doctor.httpclient import ForbiddenError, HttpClient, HttpError, NotFoundError
from repo_doctor.i18n import _

BROAD_SCOPES = {
    "repo",
    "public_repo",
    "admin:org",
    "write:org",
    "admin:repo_hook",
    "delete_repo",
    "workflow",
    "write:packages",
    "delete:packages",
    "admin:public_key",
    "admin:gpg_key",
    "user",
    "admin:enterprise",
}


def api_base(url: str | None) -> str:
    if not url or url.rstrip("/") in ("https://api.github.com", "https://github.com"):
        return "https://api.github.com"
    url = url.rstrip("/")
    return url if url.endswith("/api/v3") else url + "/api/v3"


def auth_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _merge_methods(d: dict[str, object]) -> tuple[str, ...]:
    flags = (
        ("merge", "allow_merge_commit"),
        ("squash", "allow_squash_merge"),
        ("rebase", "allow_rebase_merge"),
    )
    return tuple(method for method, key in flags if d.get(key))


def _run_check(run: dict[str, Any]) -> CICheck:
    """Check run (Actions) → řádek CI: dokud neskončil, běží; neutral a skipped se berou jako prošlé."""
    state: CIState
    if run.get("status") != "completed":
        state = "pending"
    elif run.get("conclusion") in ("success", "neutral", "skipped"):
        state = "success"
    else:
        state = "failure"
    return CICheck(str(run.get("name") or "?"), state, str(run.get("html_url") or ""))


def _status_checks(statuses: dict[str, Any]) -> list[CICheck]:
    """Klasické commit statusy (třetí strany) jako řádky CI."""
    mapped: dict[str, CIState] = {"success": "success", "failure": "failure", "error": "failure"}
    return [
        CICheck(
            str(s.get("context") or "?"),
            mapped.get(str(s.get("state")), "pending"),
            str(s.get("target_url") or ""),
        )
        for s in statuses.get("statuses") or []
    ]


def _pr(d: dict[str, object]) -> PullRequestInfo:
    head, base = sub_dict(d, "head"), sub_dict(d, "base")
    mergeable = d.get("mergeable")
    return PullRequestInfo(
        number=int_field(d, "number"),
        url=str(d.get("html_url") or ""),
        state="merged" if d.get("merged") or d.get("merged_at") else (
            "open" if d.get("state") == "open" else "closed"
        ),
        title=str(d.get("title") or ""),
        head_branch=str(head.get("ref") or ""),
        base_branch=str(base.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        mergeable=mergeable if isinstance(mergeable, bool) else None,
    )  # fmt: skip


def _repo(d: dict[str, object]) -> ForgeRepo:
    return ForgeRepo(
        full_name=str(d.get("full_name", "")),
        default_branch=str(d["default_branch"]) if d.get("default_branch") else None,
        private=bool(d.get("private")),
        archived=bool(d.get("archived")),
        html_url=str(d.get("html_url") or ""),
        clone_ssh=str(d.get("ssh_url") or ""),
        clone_https=str(d.get("clone_url") or ""),
        pushed_at=parse_time(d.get("pushed_at")),
        fork=bool(d.get("fork")),
        description=str(d.get("description") or ""),
        id=str(d.get("id") or ""),
        stars=opt_int(d.get("stargazers_count")),
        forks=opt_int(d.get("forks_count")),
        open_issues=opt_int(d.get("open_issues_count")),
        topics=str_tuple(d.get("topics")),
        created_at=parse_time(d.get("created_at")),
        merge_methods=_merge_methods(d),
    )


class GitHubForge(Forge):
    kind = "github"

    def __init__(
        self, config: ForgeConfig, client: HttpClient, *, authenticated: bool = True
    ) -> None:
        super().__init__(config, client)
        self.authenticated = authenticated

    async def list_repos(self) -> list[ForgeRepo]:
        params: dict[str, str | int]
        if self.config.org:
            url, params = f"/orgs/{quote(self.config.org)}/repos", {"per_page": 100, "type": "all"}
        elif self.authenticated:
            url, params = (
                "/user/repos",
                {"per_page": 100, "affiliation": "owner,collaborator,organization_member"},
            )
        elif self.config.user:
            url, params = f"/users/{quote(self.config.user)}/repos", {"per_page": 100}
        else:
            return []
        return [_repo(d) for d in await self.client.paginate(url, params=params)]

    async def get_repo(self, owner_path: str) -> ForgeRepo:
        return _repo(await self.client.get_json(f"/repos/{owner_path}"))

    async def branch_head(self, repo: ForgeRepo, branch: str) -> tuple[str | None, bool | None]:
        data = await self.client.get_json(
            f"/repos/{repo.full_name}/branches/{quote(branch, safe='')}"
        )
        sha = (data.get("commit") or {}).get("sha")
        protected = data.get("protected")
        return sha, bool(protected) if protected is not None else None

    async def latest_ci_status(self, repo: ForgeRepo, branch: str, sha: str | None) -> CIStatus:
        try:
            data = await self.client.get_json(
                f"/repos/{repo.full_name}/actions/runs", params={"branch": branch, "per_page": 1}
            )
        except (NotFoundError, ForbiddenError):
            return CIStatus("unknown", branch)
        runs = data.get("workflow_runs") or []
        if not runs:
            return CIStatus("none", branch)
        run = runs[0]
        conclusion = run.get("conclusion")
        if run.get("status") != "completed":
            state: CIState = "pending"
        elif conclusion in ("failure", "timed_out", "startup_failure"):
            state = "failure"
        elif conclusion == "success":
            state = "success"
        else:
            state = "none"
        return CIStatus(
            state, branch, str(run.get("run_number") or ""), str(run.get("html_url") or "")
        )

    async def find_pr(self, repo: ForgeRepo, head: str) -> PullRequestInfo | None:
        owner = repo.full_name.split("/", 1)[0]
        items = await self.client.get_json(
            f"/repos/{repo.full_name}/pulls",
            params={
                "head": f"{owner}:{head}",
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 1,
            },
            ttl=0,
        )
        return _pr(items[0]) if isinstance(items, list) and items else None

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
        """Check runs (Actions) i klasické commit statusy posledního commitu PR."""
        sha = pr.head_sha
        if not sha:
            return CIStatus("unknown", pr.head_branch)
        try:
            runs = await self.client.get_json(
                f"/repos/{repo.full_name}/commits/{sha}/check-runs",
                params={"per_page": 100},
                ttl=0,
            )
            statuses = await self.client.get_json(
                f"/repos/{repo.full_name}/commits/{sha}/status", ttl=0
            )
        except (NotFoundError, ForbiddenError):
            return CIStatus("unknown", pr.head_branch)
        items = runs.get("check_runs") or []
        failed = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
        done = {"success", "neutral", "skipped"}
        states = [str(r.get("conclusion")) if r.get("status") == "completed" else "pending"
                  for r in items]  # fmt: skip
        legacy = str(statuses.get("state") or "") if statuses.get("total_count") else ""
        if any(x in failed for x in states) or legacy in ("failure", "error"):
            state: CIState = "failure"
        elif any(x == "pending" for x in states) or legacy == "pending":
            state = "pending"
        elif (states and all(x in done for x in states)) or legacy == "success":
            state = "success"
        elif not states and not legacy:
            state = "none"
        else:
            state = "unknown"
        return CIStatus(
            state, pr.head_branch, "", pr.url, [*map(_run_check, items), *_status_checks(statuses)]
        )

    async def merge_pr(self, repo: ForgeRepo, pr: PullRequestInfo, method: str | None) -> None:
        payload: dict[str, str] = {"sha": pr.head_sha}
        if method:
            payload["merge_method"] = method
        await self.client.request_json(
            "PUT", f"/repos/{repo.full_name}/pulls/{pr.number}/merge", json_body=payload, ttl=0
        )

    async def security_alerts(self, repo: ForgeRepo) -> dict[str, int] | None:
        result: dict[str, int] = {}
        for kind, path in (
            ("dependabot", "dependabot/alerts"),
            ("secret_scanning", "secret-scanning/alerts"),
        ):
            try:
                items = await self.client.paginate(
                    f"/repos/{repo.full_name}/{path}",
                    params={"state": "open", "per_page": 100},
                    max_pages=5,
                )
            except (NotFoundError, ForbiddenError):
                continue
            result[kind] = len(items)
        return result or None

    async def open_prs(self, repo: ForgeRepo) -> list[PullRequest]:
        items = await self.client.paginate(
            f"/repos/{repo.full_name}/issues",
            params={"state": "open", "per_page": 100},
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

    async def resolve_ref(self, owner_repo: str, ref: str) -> str | None:
        """SHA commitu, na který ukazuje tag/větev akce (pro připnutí v ci-security)."""
        if not valid_owner_path(owner_repo) or not ref or ref.startswith("-"):
            return None
        try:
            data = await self.client.get_json(f"/repos/{owner_repo}/commits/{quote(ref, safe='')}")
        except HttpError:
            return None
        sha = str(data.get("sha", "")) if isinstance(data, dict) else ""
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None

    async def _fetch_noreply(self) -> str | None:
        if not self.authenticated:
            return None
        user = await self.client.get_json("/user")
        uid, login = user.get("id"), user.get("login")
        if not uid or not login:
            return None
        host = urlsplit(api_base(self.config.url)).hostname or "api.github.com"
        domain = "users.noreply.github.com" if host == "api.github.com" else f"users.noreply.{host}"
        return f"{uid}+{login}@{domain}"

    async def test_connection(self) -> ConnectionReport:
        warnings: list[str] = []
        try:
            if self.authenticated:
                user = await self.client.get_json("/user")
                login = str(user.get("login", ""))
                scopes_header = self.client.last_headers.get("x-oauth-scopes")
            else:
                if not self.config.user and not self.config.org:
                    return ConnectionReport(
                        False, error=_("bez tokenu je potřeba vyplnit uživatele nebo organizaci")
                    )
                target = (
                    f"/orgs/{quote(self.config.org)}"
                    if self.config.org
                    else f"/users/{quote(self.config.user or '')}"
                )
                user = await self.client.get_json(target)
                login = str(user.get("login", ""))
                scopes_header = None
                warnings.append(_("bez tokenu: jen veřejná data a limit 60 dotazů za hodinu"))
            repos = await self.list_repos()
        except HttpError as err:
            return ConnectionReport(False, error=str(err))
        scopes = [s.strip() for s in (scopes_header or "").split(",") if s.strip()]
        broad = sorted(set(scopes) & BROAD_SCOPES)
        if broad:
            warnings.append(
                _(
                    "token má zbytečně široká práva ({scopes}); pro sken stačí fine-grained "
                    "token s Metadata, Contents, Actions a Dependabot alerts: read-only "
                    "(PR a merge z odeslání oprav navíc Contents a Pull requests: read & write)"
                ).format(scopes=", ".join(broad))
            )
        return ConnectionReport(
            True, user=login, repo_count=len(repos), scopes=scopes, warnings=warnings
        )

    async def public_visibility(self, owner_path: str) -> str:
        """Neautentizovaný dotaz: 200 → veřejné; 404 → privátní nebo neexistuje (neznámo)."""
        try:
            data = await self.client.get_json(f"/repos/{owner_path}")
        except HttpError:
            return "unknown"
        return "private" if data.get("private") else "public"
