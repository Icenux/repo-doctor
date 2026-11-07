"""Režim MCP serveru (`repo-doctor mcp`) – nad stejným jádrem jako TUI a CLI.

Výchozí stav je jen čtení: server nic neopravuje, nefetchuje, neklonuje a nepoužívá síť
(`[mcp] network = false` → sken je offline). Nástroje vidí jen repa pod sledovanými složkami
(`[mcp] restrict_to_roots`). Zápisové nástroje (`[mcp] writes`: opravná větev, allowlist,
fast-forward, push + PR) se zapínají po jednom; vypnutý se vůbec neregistruje. Každý má
`confirm=false` = jen náhled. Výstupy jsou strukturované (pydantic), stránkované a omezené
velikostí; tajemství a e-maily v nich jsou maskované stejně jako v reportech (nálezy je nesou
už maskované, navíc vše prochází redakčním registrem).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from repo_doctor import __version__, checkdocs, fixes, history, paths, publish, secrets_scan, sync
from repo_doctor.checks import all_checks
from repo_doctor.checks.base import RepoContext
from repo_doctor.config import (
    AllowEntry,
    Config,
    ConfigError,
    ConfigStore,
    MCPWrite,
    RootConfig,
    effective_for_repo,
    load_repo_overrides,
)
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.masking import redact
from repo_doctor.models import Category, Finding, Patch, RepoResult, ScanResult, Severity
from repo_doctor.scoring import band

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

MAX_LIMIT = 100
MAX_TEXT = 500
MAX_PREVIEW = 8000
StateFilter = Literal["all", "problems", "clean", "dirty", "unpushed", "unchecked"]


def _safe_diff(diff: str) -> str:
    """Náhled změn pro klienta: řádky, které vypadají jako tajemství (i kontextové), se schovají.

    `redact` zná jen hodnoty, které už scanner našel; diff ale nese i okolní řádky souboru."""
    out: list[str] = []
    for line in diff.splitlines():
        # kontroluje se tělo každého řádku, i „hlavičkového“: odstraněný řádek `-- KEY=…` vypadá
        # v diffu jako `---…` a při rozlišování hlaviček by se přeskočil
        body = line[1:] if line[:1] in "+- " else line
        if next(secrets_scan.scan_line(body, "x.env", 0), None):
            out.append(f"{line[:1]}[line hidden: possible secret]")
        else:
            out.append(line)
    return "\n".join(out)


def _text(value: str | None, limit: int = MAX_TEXT) -> str:
    text = redact(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------ modely výstupů
class Page(BaseModel):
    total: int = Field(description="Number of matching items before paging.")
    offset: int
    limit: int
    next_offset: int | None = Field(description="Offset of the next page, or null when done.")


class RepoSummary(BaseModel):
    name: str
    path: str
    root: str
    score: int | None = Field(description="0–100 health score; null when the repo was unchecked.")
    band: str
    high: int
    medium: int
    low: int
    branch: str | None
    uncommitted: int
    unpushed: int
    forge: str | None
    visibility: str


class RepoList(BaseModel):
    page: Page
    repos: list[RepoSummary]


class RemoteSummary(BaseModel):
    name: str
    url: str = Field(description="Remote URL without credentials.")
    forge: str | None


class RepoStatus(BaseModel):
    name: str
    path: str
    branch: str | None
    detached: bool
    head: str | None
    default_branch: str | None
    uncommitted: int
    unpushed: int
    last_commit: str | None
    untrusted_owner: bool
    remotes: list[RemoteSummary]
    errors: dict[str, str]


class FindingOut(BaseModel):
    repo: str
    check_id: str
    severity: str
    category: str
    title: str
    message: str
    location: str | None = None
    snippet: str | None = Field(None, description="Already masked excerpt (never the raw secret).")
    fixable: bool
    fingerprint: str = Field(description="Stable id of the finding; pass it to `allowlist_add`.")


class WriteOut(BaseModel):
    action: str
    applied: bool = Field(description="false = preview only; repeat the call with confirm=true.")
    summary: str
    preview: str = Field("", description="Diff or plan of what the action does (masked).")
    branch: str | None = None
    pull_request: str | None = None


class FindingList(BaseModel):
    page: Page
    findings: list[FindingOut]


class ScanSummaryOut(BaseModel):
    available: bool
    finished_at: str | None = None
    offline: bool | None = None
    repos: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    health: int | None = None
    warnings: list[str] = Field(default_factory=list)


class CheckExplanation(BaseModel):
    check_id: str
    title: str
    severity: str
    category: str
    text: str = Field(description="Markdown documentation (in Czech).")


# ------------------------------------------------------------------ jádro
def _page(total: int, offset: int, limit: int) -> Page:
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    nxt = offset + limit if offset + limit < total else None
    return Page(total=total, offset=offset, limit=limit, next_offset=nxt)


def _summary(repo: RepoResult, no_pulse_days: int) -> RepoSummary:
    counts = repo.counts()
    return RepoSummary(
        name=repo.name,
        path=repo.path,
        root=repo.root,
        score=repo.score,
        band=band(repo, no_pulse_days).value,
        high=counts[Severity.HIGH],
        medium=counts[Severity.MEDIUM],
        low=counts[Severity.LOW],
        branch=repo.state.branch,
        uncommitted=repo.state.uncommitted,
        unpushed=repo.state.unpushed,
        forge=repo.forge,
        visibility=repo.visibility,
    )


def _finding(repo: RepoResult, f: Finding) -> FindingOut:
    where = f.location.render()
    return FindingOut(
        repo=repo.name,
        check_id=f.check_id,
        severity=f.severity.value,
        category=f.category.value,
        title=_text(f.title, 200),
        message=_text(f.message),
        location=_text(where, 300) or None,
        snippet=_text(f.snippet, 200) or None,
        fixable=f.fixable,
        fingerprint=f.fingerprint(repo.name),
    )


def _sev(value: str | None) -> Severity | None:
    return Severity.parse(value) if value else None


class RepoDoctorTools:
    """Logika nástrojů nezávislá na MCP (testovatelná přímo)."""

    def __init__(
        self,
        config: Config,
        *,
        load: Callable[[], ScanResult | None] = history.load_last_scan,
        save: Callable[[ScanResult], object] | None = history.save_last_scan,
        store: ConfigStore | None = None,
    ) -> None:
        self.config = config
        self._store = store
        self._load = load
        self._save = save
        self._result: ScanResult | None = None

    # -- data
    def result(self) -> ScanResult | None:
        if self._result is None:
            self._result = self._load()
        return self._result

    def _require(self) -> ScanResult:
        result = self.result()
        if result is None:
            raise ValueError("No scan result yet – call the `scan` tool first.")
        return result

    def _under_roots(self, path: str | Path) -> bool:
        """Je cesta ve sledované složce (nebo je to ta složka)? Symlinky se rozbalí."""
        target = Path(path).expanduser().resolve()
        return any(
            target.is_relative_to(paths.expand_path(root.path).resolve())
            for root in self.config.roots
            if root.enabled
        )

    def _visible(self) -> list[RepoResult]:
        repos = list(self._require().repos)
        if self.config.mcp.restrict_to_roots:
            repos = [r for r in repos if self._under_roots(r.path)]
        return repos

    def _repo(self, repo: str) -> RepoResult:
        matches = [r for r in self._visible() if repo in (r.name, r.path)]
        if not matches:
            raise ValueError(f"Unknown repository {repo!r} – use `list_repos` to see names.")
        if len(matches) > 1:
            raise ValueError(f"Repository name {repo!r} is ambiguous – pass the full path.")
        return matches[0]

    # -- nástroje
    def list_repos(
        self,
        root: str | None = None,
        state: StateFilter = "all",
        min_score: int | None = None,
        max_score: int | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> RepoList:
        repos = self._visible()
        if root:
            repos = [
                r for r in repos if r.root == root or r.path.startswith(root.rstrip("/") + "/")
            ]
        checks: dict[str, Callable[[RepoResult], bool]] = {
            "all": lambda r: True,
            "problems": lambda r: bool(r.findings),
            "clean": lambda r: not r.findings,
            "dirty": lambda r: r.state.uncommitted > 0,
            "unpushed": lambda r: r.state.unpushed > 0,
            "unchecked": lambda r: r.score is None,
        }
        repos = [r for r in repos if checks[state](r)]
        if min_score is not None:
            repos = [r for r in repos if r.score is not None and r.score >= min_score]
        if max_score is not None:
            repos = [r for r in repos if r.score is not None and r.score <= max_score]
        repos.sort(key=lambda r: (101 if r.score is None else r.score, r.name))
        page = _page(len(repos), offset, limit)
        days = self.config.limits.no_pulse_days
        items = repos[page.offset : page.offset + page.limit]
        return RepoList(page=page, repos=[_summary(r, days) for r in items])

    def repo_status(self, repo: str) -> RepoStatus:
        r = self._repo(repo)
        s = r.state
        return RepoStatus(
            name=r.name,
            path=r.path,
            branch=s.branch,
            detached=s.detached,
            head=s.head,
            default_branch=s.default_branch,
            uncommitted=s.uncommitted,
            unpushed=s.unpushed,
            last_commit=s.last_commit.isoformat() if s.last_commit else None,
            untrusted_owner=r.untrusted_owner,
            remotes=[
                RemoteSummary(name=m.name, url=_text(m.url, 300), forge=m.forge) for m in r.remotes
            ],
            errors={k: _text(v, 300) for k, v in r.errors.items()},
        )

    def repo_findings(
        self,
        repo: str,
        min_severity: str | None = None,
        category: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> FindingList:
        r = self._repo(repo)
        return self._findings([(r, f) for f in r.findings], min_severity, category, offset, limit)

    def search_findings(
        self,
        check_id: str | None = None,
        text: str | None = None,
        min_severity: str | None = None,
        category: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> FindingList:
        pairs = [(r, f) for r in self._visible() for f in r.findings]
        if check_id:
            pairs = [(r, f) for r, f in pairs if f.check_id == check_id]
        if text:
            q = text.lower()
            pairs = [
                (r, f)
                for r, f in pairs
                if q in f.message.lower() or q in f.title.lower() or q in r.name.lower()
            ]
        return self._findings(pairs, min_severity, category, offset, limit)

    def _findings(
        self,
        pairs: list[tuple[RepoResult, Finding]],
        min_severity: str | None,
        category: str | None,
        offset: int,
        limit: int,
    ) -> FindingList:
        sev = _sev(min_severity)
        if sev is not None:
            pairs = [(r, f) for r, f in pairs if f.severity.rank >= sev.rank]
        if category:
            cat = Category(category.lower())
            pairs = [(r, f) for r, f in pairs if f.category is cat]
        pairs.sort(key=lambda p: (-p[1].severity.rank, p[0].name, p[1].check_id))
        page = _page(len(pairs), offset, limit)
        items = pairs[page.offset : page.offset + page.limit]
        return FindingList(page=page, findings=[_finding(r, f) for r, f in items])

    def last_scan_summary(self) -> ScanSummaryOut:
        result = self.result()
        if result is None:
            return ScanSummaryOut(available=False)
        s = result.summary(self.config.limits.no_pulse_days)
        return ScanSummaryOut(
            available=True,
            finished_at=(result.finished_at or result.started_at).isoformat(),
            offline=result.offline,
            repos=s.repos,
            high=s.by_severity[Severity.HIGH],
            medium=s.by_severity[Severity.MEDIUM],
            low=s.by_severity[Severity.LOW],
            health=s.health,
            warnings=[_text(w, 300) for w in result.warnings[:20]],
        )

    async def scan(self, repo: str | None = None, timeout_s: float = 120.0) -> ScanSummaryOut:
        from repo_doctor.discovery import DiscoveredRepo
        from repo_doctor.scanner import Scanner, ScanOptions

        offline = not self.config.mcp.network
        scanner = Scanner(self.config, ScanOptions(offline=offline, fetch=False))
        targets: list[DiscoveredRepo] | None = None
        roots: list[RootConfig] | None = None
        if repo:
            current = self.result()
            known = next(
                (r for r in (current.repos if current else []) if repo in (r.name, r.path)),
                None,
            )
            path = Path(known.path if known else repo).expanduser()
            if self.config.mcp.restrict_to_roots and not self._under_roots(path):
                raise ValueError(
                    f"{repo!r} is outside the watched folders. Add its folder in "
                    "repo-doctor first, or turn off [mcp] restrict_to_roots."
                )
            if not (path / ".git").exists():
                raise ValueError(
                    f"{repo!r} is not a known repository or a path to a git repository."
                )
            targets = [
                DiscoveredRepo(
                    path=path.resolve(),
                    root=known.root if known else str(path.parent),
                    display=str(path),
                )
            ]
            roots = []
        timeout_s = max(1.0, min(timeout_s, 1800.0))
        try:
            fresh = await asyncio.wait_for(scanner.run(roots, targets), timeout_s)
        except TimeoutError as err:
            scanner.cancel()
            raise ValueError(f"Scan did not finish within {timeout_s:.0f} s.") from err
        current = self.result()
        if repo and current is not None:
            updated = {r.path: r for r in fresh.repos}
            current.repos = [updated.pop(r.path, r) for r in current.repos] + list(updated.values())
            fresh = current
        self._result = fresh
        if self._save is not None:
            self._save(fresh)
        return self.last_scan_summary()

    def explain_check(self, check_id: str) -> CheckExplanation:
        check = next((c for c in all_checks() if c.id == check_id), None)
        doc = checkdocs.load(check_id)
        if check is None or doc is None:
            ids = ", ".join(sorted(c.id for c in all_checks()))
            raise ValueError(f"Unknown check {check_id!r}. Known checks: {ids}")
        return CheckExplanation(
            check_id=check.id,
            title=check.title,
            severity=check.severity.value,
            category=check.category.value,
            text=doc.text,
        )

    # -- zápisy (každý jde zapnout zvlášť v `[mcp] writes`; bez confirm=true jen náhled)
    def _write_target(self, action: MCPWrite, repo: str) -> RepoResult:
        if action not in self.config.mcp.writes:
            raise ValueError(
                f"The write action {action!r} is disabled. The user can enable it in "
                "repo-doctor Settings (MCP) or in `[mcp] writes`."
            )
        r = self._repo(repo)
        if not self._under_roots(r.path):  # zápis nikdy mimo sledované složky
            raise ValueError(f"{repo!r} is outside the watched folders; refusing to change it.")
        return r

    def _repo_context(self, r: RepoResult) -> RepoContext:
        path = Path(r.path)
        config = self.config
        with contextlib.suppress(ConfigError):
            config = effective_for_repo(config, load_repo_overrides(path))
        return RepoContext(
            path=path, name=r.name, root=r.root, config=config, git=Git(path), offline=True
        )

    def fix_branch(
        self, repo: str, check_ids: list[str] | None = None, confirm: bool = False
    ) -> WriteOut:
        r = self._write_target("fix_branch", repo)
        ctx = self._repo_context(r)
        try:
            patches = fixes.plan(ctx, [f for f in r.findings if f.fixable])
        except (GitError, OSError) as err:
            raise ValueError(f"Fixes cannot be prepared: {_text(str(err), 300)}") from err
        selected: list[Patch] = []
        for patch in patches:  # od každé kontroly první varianta, jako výchozí volba v TUI
            if (not check_ids or patch.check_id in check_ids) and all(
                patch.check_id != p.check_id for p in selected
            ):
                selected.append(patch)
        if not selected:
            raise ValueError("No automatically fixable findings match (see `repo_findings`).")
        ids = ", ".join(p.check_id for p in selected)
        preview = _text(_safe_diff("".join(fixes.render_diff(p) for p in selected)), MAX_PREVIEW)
        if not confirm:
            return WriteOut(
                action="fix_branch",
                applied=False,
                summary=f"Would apply fixes for: {ids}. Nothing changed; use confirm=true.",
                preview=preview,
            )
        try:
            result = fixes.apply(ctx.git, selected)
        except (fixes.FixRefused, GitError) as err:
            raise ValueError(_text(str(err), 400)) from err
        return WriteOut(
            action="fix_branch",
            applied=True,
            summary=f"Fixes applied: {ids}. Nothing was pushed.",
            preview=preview,
            branch=result.branch,
        )

    def allowlist_add(
        self, repo: str, fingerprint: str, reason: str, confirm: bool = False
    ) -> WriteOut:
        r = self._write_target("allowlist", repo)
        finding = next((f for f in r.findings if f.fingerprint(r.name) == fingerprint), None)
        if finding is None:
            raise ValueError("Unknown fingerprint – take it from `repo_findings`.")
        if finding.category is Category.SECURITY:
            # allowlist umlčí i pre-push hlídače, takže by šel řetězem obejít (allowlist → publish)
            raise ValueError(
                "Security findings can't be allowlisted over MCP – the user decides that "
                "in the repo-doctor app (finding card, key a)."
            )
        reason = _text(reason.strip(), 200)
        if not reason:
            raise ValueError("A reason is required.")
        summary = f"Would hide {finding.check_id} in {r.name} (reason: {reason})."
        if not confirm:
            return WriteOut(
                action="allowlist", applied=False, summary=summary + " Use confirm=true."
            )
        store = self._store or ConfigStore()
        try:
            # vždy znovu z disku: TUI nebo editor mohl config.toml mezitím změnit (a zápisy vypnout)
            fresh = store.load()
            if "allowlist" not in fresh.mcp.writes:
                raise ValueError(
                    "The write action 'allowlist' was disabled in the meantime (Settings → MCP)."
                )
            saved = store.add_allow(
                AllowEntry(hash=fingerprint, reason=reason, check=finding.check_id, repo=r.name)
            )
            self.config = self.config.model_copy(update={"allowlist": saved.allowlist})
        except ConfigError as err:
            raise ValueError(_text(str(err), 300)) from err
        r.findings = [f for f in r.findings if f.fingerprint(r.name) != fingerprint]
        r.allowlisted += 1
        if self._save is not None and self._result is not None:
            self._save(self._result)
        return WriteOut(action="allowlist", applied=True, summary=summary.replace("Would", "Did"))

    def sync_ff(self, repo: str, confirm: bool = False) -> WriteOut:
        r = self._write_target("sync", repo)
        path = Path(r.path)
        st = sync.status(path)  # bez fetch: jen to, co už je staženo
        if not st.can_fast_forward:
            raise ValueError(
                _text(st.reason or f"Nothing to fast-forward (state: {st.state}).", 300)
            )
        files = ", ".join(st.files[:20])
        summary = f"Fast-forward of {st.branch} by {st.behind} commit(s)."
        if not confirm:
            return WriteOut(
                action="sync",
                applied=False,
                summary=summary + " Nothing changed; use confirm=true.",
                preview=_text(f"Changes files: {files}", MAX_PREVIEW),
            )
        result = sync.fast_forward([path])
        if result.skipped:
            raise ValueError(_text(result.skipped[0][1], 300))
        return WriteOut(
            action="sync",
            applied=True,
            summary=summary + " Nothing was pushed.",
            branch=st.branch,
        )

    async def publish_fix(
        self, repo: str, branch: str | None = None, confirm: bool = False
    ) -> WriteOut:
        r = self._write_target("publish", repo)
        git = Git(Path(r.path))
        try:
            plan = await asyncio.to_thread(publish.plan_push, git, self.config, branch)
            lines = [f"{c.short} {c.subject}" for c in plan.commits]
            preview = _text(
                f"push {plan.branch} -> {plan.remote} ({plan.remote_url})\n" + "\n".join(lines),
                MAX_PREVIEW,
            )
            if not confirm:
                return WriteOut(
                    action="publish",
                    applied=False,
                    summary="Would push the fix branch and open a PR/MR. Use confirm=true.",
                    preview=preview,
                    branch=plan.branch,
                )
            await asyncio.to_thread(publish.apply_push, git, plan, self.config)
            try:
                url = await self._open_pr(git, plan.branch)
            except publish.PublishRefused as err:
                return WriteOut(
                    action="publish",
                    applied=True,
                    summary=f"Branch pushed, but the PR was not created: {_text(str(err), 300)}",
                    preview=preview,
                    branch=plan.branch,
                )
        except publish.PublishRefused as err:
            raise ValueError(_text(str(err), 500)) from err
        return WriteOut(
            action="publish",
            applied=True,
            summary="Branch pushed and PR/MR created. Nothing was merged.",
            preview=preview,
            branch=plan.branch,
            pull_request=url,
        )

    async def _open_pr(self, git: Git, branch: str) -> str:
        async def work(hosting: publish.Hosting) -> str:
            pr_plan = await publish.plan_pr(git, hosting, self.config, branch)
            pr = await publish.apply_pr(git, hosting, self.config, pr_plan)
            return pr.url

        return await publish.with_hosting(git, self.config, work)


# ------------------------------------------------------------------ MCP vrstva
INSTRUCTIONS = """\
repo-doctor audits the user's local git repositories (secrets, hygiene, git state, hosting).
It only sees repositories under the folders the user watches. By default this server is
read-only: it never modifies repositories, never fetches or clones, and runs offline unless
the user enabled `[mcp] network = true`. Secrets and e-mail addresses are always masked.
Start with `last_scan_summary`; if no scan is available, call `scan`. Then use `list_repos`,
`repo_findings` and `explain_check`.
"""

WRITE_INSTRUCTIONS = """\
The user also enabled these write tools: {tools}. Each one only previews what it would do
until you call it again with confirm=true – show the preview to the user first and ask.
Writes never force-push, never merge and never touch uncommitted work.
"""


def build_server(tools: RepoDoctorTools) -> MCPServer:
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    writes = tools.config.mcp.writes
    names = {
        "fix_branch": "fix_branch",
        "allowlist": "allowlist_add",
        "sync": "sync_ff",
        "publish": "publish_fix",
    }
    instructions = INSTRUCTIONS
    if writes:
        instructions += WRITE_INSTRUCTIONS.format(tools=", ".join(names[w] for w in writes))
    server = MCPServer(name="repo-doctor", version=__version__, instructions=instructions)
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    wr = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=ro)
    def list_repos(
        root: str | None = None,
        state: StateFilter = "all",
        min_score: int | None = None,
        max_score: int | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> RepoList:
        """List scanned repositories, worst score first.

        Filters: `root` (a watched folder as configured, or a path prefix), `state`
        (all | problems | clean | dirty | unpushed | unchecked), `min_score` / `max_score`.
        Paged with `offset` and `limit` (max 100); follow `page.next_offset`.

        Example: list_repos(state="problems", max_score=60)
        """
        return tools.list_repos(root, state, min_score, max_score, offset, limit)

    @server.tool(annotations=ro)
    def repo_status(repo: str) -> RepoStatus:
        """Git state of one repository from the last scan: branch, HEAD, uncommitted and
        unpushed changes, remotes (without credentials) and scan errors.

        `repo` is the repository name or its full path.
        Example: repo_status(repo="api-gateway")
        """
        return tools.repo_status(repo)

    @server.tool(annotations=ro)
    def repo_findings(
        repo: str,
        min_severity: str | None = None,
        category: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> FindingList:
        """Findings of one repository, most severe first. Secrets are masked.

        `min_severity`: high | medium | low. `category`: security | maintenance | git | forge.
        Example: repo_findings(repo="infra-notes", min_severity="high")
        """
        return tools.repo_findings(repo, min_severity, category, offset, limit)

    @server.tool(annotations=ro)
    def search_findings(
        check_id: str | None = None,
        text: str | None = None,
        min_severity: str | None = None,
        category: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> FindingList:
        """Search findings across all repositories by check id and/or text.

        Example: search_findings(check_id="secrets-history")
        Example: search_findings(text="Dockerfile", min_severity="medium")
        """
        return tools.search_findings(check_id, text, min_severity, category, offset, limit)

    @server.tool(annotations=ro)
    def last_scan_summary() -> ScanSummaryOut:
        """Summary of the last scan: time, repo count, findings by severity, overall health.

        `available` is false when no scan has been run yet – call `scan` then.
        """
        return tools.last_scan_summary()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    )
    async def scan(repo: str | None = None, timeout_s: float = 120.0) -> ScanSummaryOut:
        """Run a fresh scan of all watched folders, or of one repository (`repo` = name or
        path). Offline unless `[mcp] network = true`. Never fetches, fixes or changes repos;
        only repo-doctor's own cache is updated. Stops after `timeout_s` seconds.

        Example: scan(repo="api-gateway", timeout_s=60)
        """
        return await tools.scan(repo, timeout_s)

    @server.tool(annotations=ro)
    def explain_check(check_id: str) -> CheckExplanation:
        """Documentation of a check: what it detects, why it matters and how to fix it
        (Markdown, in Czech).

        Example: explain_check(check_id="secrets-history")
        """
        return tools.explain_check(check_id)

    if "fix_branch" in writes:

        @server.tool(annotations=wr)
        def fix_branch(
            repo: str, check_ids: list[str] | None = None, confirm: bool = False
        ) -> WriteOut:
            """WRITE. Apply automatic fixes of a repository into a NEW branch
            `repo-doctor/fixes-<date>` (working tree, index and current branch stay untouched;
            nothing is pushed). Without `confirm=true` it only returns the diff.

            `check_ids` limits the fixes (default: all fixable findings). The repo must have
            no uncommitted changes. Some fixes also edit `.git/config` or hooks locally.
            Example: fix_branch(repo="api-gateway", check_ids=["env-committed"])
            """
            return tools.fix_branch(repo, check_ids, confirm)

    if "allowlist" in writes:

        @server.tool(annotations=wr)
        def allowlist_add(
            repo: str, fingerprint: str, reason: str, confirm: bool = False
        ) -> WriteOut:
            """WRITE. Hide one finding by adding it to repo-doctor's allowlist (config.toml
            only; the repository is not touched). Use the `fingerprint` from `repo_findings`
            and a short `reason`. Without `confirm=true` it only previews.

            Example: allowlist_add(repo="infra-notes", fingerprint="0123456789abcdef",
            reason="test fixture", confirm=true)
            """
            return tools.allowlist_add(repo, fingerprint, reason, confirm)

    if "sync" in writes:

        @server.tool(
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, openWorldHint=False
            )
        )
        def sync_ff(repo: str, confirm: bool = False) -> WriteOut:
            """WRITE. Fast-forward a clean, behind repository to its upstream
            (`git merge --ff-only`; never a merge commit, rebase, reset or push). It does
            not fetch, so it only catches up to what is already downloaded. Without
            `confirm=true` it only previews.

            Example: sync_ff(repo="api-gateway", confirm=true)
            """
            return tools.sync_ff(repo, confirm)

    if "publish" in writes:

        @server.tool(
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, openWorldHint=True
            )
        )
        async def publish_fix(
            repo: str, branch: str | None = None, confirm: bool = False
        ) -> WriteOut:
            """WRITE, uses the network. Push the repo-doctor fix branch (default: the newest
            `repo-doctor/fixes-*`) to its remote and open a PR/MR. Never forces, never merges;
            a secret scan runs before the push. Without `confirm=true` it only previews the
            commits that would be sent.

            Example: publish_fix(repo="api-gateway", confirm=true)
            """
            return await tools.publish_fix(repo, branch, confirm)

    return server


def run(config: Config, store: ConfigStore | None = None) -> None:
    server = build_server(RepoDoctorTools(config, store=store))
    asyncio.run(server.run_stdio_async())
