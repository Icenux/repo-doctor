"""Orchestrace skenu: discovery → lokální kontext → síťová data → kontroly → skóre.

Repozitáře běží paralelně (semafor `jobs`), git příkazy ve vláknech (`asyncio.to_thread`),
síť přes async httpx. Události (průběh) se volají vždy z vlákna event loopu.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx

from repo_doctor import __version__, paths
from repo_doctor.checks.base import (
    UNSAFE_OWNERSHIP_ID,
    Check,
    RepoContext,
    SkipCheck,
    select_checks,
)
from repo_doctor.checks.duplicates import DUPLICATE_ID, add_duplicate_findings
from repo_doctor.checks.meta import UnsafeOwnership
from repo_doctor.config import (
    Config,
    ConfigError,
    RootConfig,
    effective_for_repo,
    load_repo_overrides,
)
from repo_doctor.deps import DepsData, collect, enrich
from repo_doctor.discovery import DiscoveredRepo, discover
from repo_doctor.forges import ForgeManager
from repo_doctor.forges.github import GitHubForge
from repo_doctor.forges.urls import (
    RemoteURL,
    SshConfig,
    host_label,
    match_forge,
    parse_remote,
    web_url,
)
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.httpclient import HttpClient, HttpError, ResponseCache
from repo_doctor.i18n import _
from repo_doctor.masking import redact, redact_url_credentials
from repo_doctor.models import RemoteInfo, RepoDetails, RepoResult, RepoState, ScanResult
from repo_doctor.scoring import score
from repo_doctor.sshhelp import explain_ssh_error

log = logging.getLogger(__name__)

EventKind = Literal["discovered", "repo_started", "check", "repo_done", "warning", "finished"]


@dataclass
class ScanEvent:
    kind: EventKind
    total: int = 0
    done: int = 0
    repo: str | None = None
    check: str | None = None
    result: RepoResult | None = None
    message: str | None = None


@dataclass
class ScanOptions:
    offline: bool = False
    use_forges: bool = True
    only: list[str] | None = None
    skip: list[str] | None = None
    jobs: int = field(default_factory=lambda: os.cpu_count() or 4)
    fetch: bool = False
    now: datetime | None = None


@dataclass
class NetServices:
    """Síťové služby pro jeden sken (veřejný klient + připojené hostingy)."""

    public: HttpClient
    forges: ForgeManager
    github_public: GitHubForge | None = None

    @classmethod
    def create(
        cls,
        config: Config,
        *,
        cache_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> NetServices:
        cache = ResponseCache(cache_dir) if cache_dir else ResponseCache()
        public = HttpClient(
            namespace="public",
            cache=cache,
            ttl=config.limits.registry_cache_hours * 3600,
            transport=transport,
        )
        from repo_doctor.config import ForgeConfig
        from repo_doctor.forges import build_forge

        gh_public = build_forge(
            ForgeConfig(name="github-public", type="github"),
            token=None,
            cache=cache,
            ttl=config.limits.http_cache_minutes * 60,
            transport=transport,
        )
        return cls(
            public=public,
            forges=ForgeManager.from_config(config, cache_dir=cache_dir, transport=transport),
            github_public=gh_public if isinstance(gh_public, GitHubForge) else None,
        )

    async def aclose(self) -> None:
        await self.public.aclose()
        await self.forges.aclose()
        if self.github_public:
            await self.github_public.client.aclose()


class Scanner:
    def __init__(
        self,
        config: Config,
        options: ScanOptions | None = None,
        *,
        on_event: Callable[[ScanEvent], None] | None = None,
        net: NetServices | None = None,
        ssh_config: SshConfig | None = None,
    ) -> None:
        self.config = config
        self.options = options or ScanOptions()
        self.on_event = on_event or (lambda e: None)
        self._net = net
        self._own_net = net is None
        self.ssh_config = ssh_config
        self.checks: list[Check] = select_checks(
            self.options.only, self.options.skip, config.checks.disabled
        )
        self._cancelled = False
        self._tasks: list[asyncio.Task[RepoResult | None]] = []

    # ------------------------------------------------------------------ API
    def cancel(self) -> None:
        self._cancelled = True
        for t in self._tasks:
            t.cancel()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def run(
        self,
        roots: list[RootConfig] | None = None,
        repos: list[DiscoveredRepo] | None = None,
    ) -> ScanResult:
        roots = roots if roots is not None else self.config.roots
        now = self.options.now or datetime.now(UTC)
        result = ScanResult(
            tool_version=__version__,
            started_at=now,
            offline=self.options.offline,
            roots=[r.path for r in roots if r.enabled],
        )
        full = repos is None  # duplicitní klony jen při skenu všech sledovaných složek
        if repos is None:
            found = await asyncio.to_thread(discover, roots, self.config.ignore_repos)
            repos = found.repos
            for w in found.warnings:
                result.warnings.append(w)
                self.on_event(ScanEvent("warning", message=w))
        total = len(repos)
        self.on_event(ScanEvent("discovered", total=total))
        if self.ssh_config is None:
            self.ssh_config = await asyncio.to_thread(SshConfig.load)
        if not self.options.offline and self._net is None:
            self._net = NetServices.create(self.config, cache_dir=paths.http_cache_dir())
        if self._net:
            for name, reason in self._net.forges.unavailable.items():
                msg = _("Hosting {name} je nedostupný: {reason}").format(name=name, reason=reason)
                result.warnings.append(msg)
                self.on_event(ScanEvent("warning", message=msg))
        sem = asyncio.Semaphore(max(1, self.options.jobs))
        done = 0

        async def one(repo: DiscoveredRepo) -> RepoResult | None:
            nonlocal done
            async with sem:
                if self._cancelled:
                    return None
                self.on_event(ScanEvent("repo_started", total=total, done=done, repo=repo.name))
                res = await self._scan_repo(repo, now, total, lambda: done)
                done += 1
                self.on_event(
                    ScanEvent("repo_done", total=total, done=done, repo=repo.name, result=res)
                )
                return res

        self._tasks = [asyncio.ensure_future(one(r)) for r in repos]
        try:
            outcomes = await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            if self._own_net and self._net is not None:
                await self._net.aclose()
                self._net = None
        for repo, outcome in zip(repos, outcomes, strict=True):
            if isinstance(outcome, RepoResult):
                result.repos.append(outcome)
            elif isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                msg = _("{name}: sken selhal ({error})").format(
                    name=repo.name,
                    error=f"{type(outcome).__name__}: {redact(str(outcome))[:200]}",
                )
                result.warnings.append(msg)
        result.cancelled = self._cancelled
        result.finished_at = datetime.now(UTC) if self.options.now is None else now
        result.repos.sort(key=lambda r: (r.root, r.name))
        if full and not self._cancelled and DUPLICATE_ID in {c.id for c in self.checks}:
            self._duplicates(result)
        self.on_event(ScanEvent("finished", total=total, done=done))
        return result

    def _duplicates(self, result: ScanResult) -> None:
        touched = add_duplicate_findings(result)
        allowed = {a.hash for a in self.config.allowlist}
        for repo in result.repos:
            if repo.path not in touched:
                continue
            kept = [
                f
                for f in repo.findings
                if f.check_id != DUPLICATE_ID or f.fingerprint(repo.name) not in allowed
            ]
            repo.allowlisted += len(repo.findings) - len(kept)
            repo.findings = kept
            if repo.score is not None:
                repo.score = score(kept)

    # ------------------------------------------------------------------ jedno repo
    def _untrusted(self, repo: DiscoveredRepo) -> bool:
        try:
            return Git(repo.path, timeout=self.config.limits.git_timeout_s).dubious_ownership()
        except GitError:
            return False  # timeout apod. – ukáže se dál při běžném skenu

    def _untrusted_result(self, repo: DiscoveredRepo) -> RepoResult:
        """Repo cizího vlastníka: žádné další volání gitu, jen jeden nález a bez skóre.

        `.repo-doctor.toml` z takového repa se nečte – platí jen globální konfigurace."""
        res = RepoResult(
            path=repo.display, name=repo.name, root=repo.root, score=None, untrusted_owner=True
        )
        if UNSAFE_OWNERSHIP_ID not in self.config.checks.disabled:
            res.findings = [UnsafeOwnership().for_path(str(repo.path))]
        return res

    def _context(self, repo: DiscoveredRepo, now: datetime) -> tuple[RepoContext, RepoResult]:
        config = self.config
        res = RepoResult(path=repo.display, name=repo.name, root=repo.root)
        try:
            config = effective_for_repo(config, load_repo_overrides(repo.path))
        except ConfigError as err:
            res.errors["config"] = str(err)
        git = Git(repo.path, timeout=config.limits.git_timeout_s)
        raw_remotes = git.remotes()
        if self.options.fetch and not self.options.offline:
            try:
                git.fetch()
            except GitError as err:
                hint = explain_ssh_error(err.stderr, raw_remotes.get("origin"))
                res.errors["fetch"] = hint or err.stderr[:200]
                if hint:
                    res.errors["fetch_detail"] = err.stderr[:300]
        remotes: list[RemoteInfo] = []
        forge_name: str | None = None
        owner_path: str | None = None
        parsed_primary: RemoteURL | None = None
        ordered = sorted(raw_remotes.items(), key=lambda kv: (kv[0] != "origin", kv[0]))
        for name, url in ordered:
            parsed = parse_remote(url, self.ssh_config)
            forge = match_forge(parsed, config.forges) if parsed else None
            remotes.append(
                RemoteInfo(
                    name=name,
                    url=redact_url_credentials(url),
                    host=parsed.host if parsed else None,
                    owner_path=parsed.path if parsed else None,
                    forge=forge.name if forge else None,
                )
            )
            if parsed and parsed_primary is None:
                parsed_primary = parsed
            if forge and forge_name is None:
                forge_name, owner_path = forge.name, parsed.path if parsed else None
        res.remotes = remotes
        res.forge = forge_name or (
            host_label(parsed_primary.host) if parsed_primary and parsed_primary.host else None
        )
        res.web_url = web_url(parsed_primary) if parsed_primary else None
        ctx = RepoContext(
            path=repo.path,
            name=repo.name,
            root=repo.root,
            config=config,
            git=git,
            now=now,
            offline=self.options.offline,
            remotes=remotes,
            forge_name=forge_name,
        )
        ctx.forge_status = None if forge_name else _("remote nepatří k připojenému hostingu")
        res.state = self._state(ctx, now)
        res.forge_info["owner_path"] = owner_path or (
            parsed_primary.path if parsed_primary else None
        )
        res.forge_info["host"] = parsed_primary.host if parsed_primary else None
        return ctx, res

    @staticmethod
    def _state(ctx: RepoContext, now: datetime) -> RepoState:
        git = ctx.git
        branch = git.current_branch()
        head = ctx.head
        last = git.last_commit_ts()
        since = int((now - timedelta(days=30)).timestamp())
        pulse = [0] * 30
        for ts in git.commit_timestamps(since):
            day = int((now.timestamp() - ts) // 86400)
            if 0 <= day < 30:
                pulse[29 - day] += 1
        unpushed = 0
        if git.remotes():
            for b in git.branches():
                unpushed += (
                    b.ahead
                    if b.upstream and not b.upstream_gone
                    else git.count_not_on_remotes(b.name)
                )
        return RepoState(
            branch=branch,
            detached=branch is None and head is not None,
            head=head,
            default_branch=ctx.default_branch,
            uncommitted=ctx.status.dirty,
            unpushed=unpushed,
            last_commit=datetime.fromtimestamp(last, UTC) if last else None,
            pulse=pulse,
            commit_count=git.commit_count(),
            root_commit=git.root_commit() if head else None,
        )

    @staticmethod
    def _details(ctx: RepoContext, now: datetime) -> RepoDetails:
        from repo_doctor import details
        from repo_doctor.licenses import project_license

        files = ctx.tracked_files
        deps = ctx.deps or collect(files, ctx.read_text)  # offline jen rozparsované lockfily
        result = details.collect(
            ctx.git, files=files, deps=deps, license=project_license(files, ctx.read_text), now=now
        )
        snap = ctx.forge_snapshot
        if snap is not None and snap.repo is not None:
            r = snap.repo
            issues = r.open_issues
            if issues is not None and snap.kind == "github":  # GitHub počítá PR mezi issues
                issues = max(0, issues - sum(1 for i in snap.open_items if i.kind == "pr"))
            result.forge_description = r.description or None
            result.forge_topics = list(r.topics)
            result.forge_stars, result.forge_forks = r.stars, r.forks
            result.forge_open_issues = issues
            result.forge_created, result.forge_pushed = r.created_at, r.pushed_at
        return result

    async def _network(self, ctx: RepoContext, res: RepoResult) -> None:
        net = self._net
        if self.options.offline or net is None:
            ctx.forge_status = _("offline režim")
            ctx.deps_status = _("offline režim")
            return
        ids = {c.id for c in self.checks}
        owner_path = res.forge_info.get("owner_path")
        if ctx.forge_name and self.options.use_forges and isinstance(owner_path, str):
            forge = net.forges.forges.get(ctx.forge_name)
            if forge is None:
                ctx.forge_status = _("hosting {forge_name} nedostupný: {reason}").format(
                    forge_name=ctx.forge_name, reason=net.forges.unavailable.get(ctx.forge_name, "")
                )
            else:
                try:
                    snap = await forge.snapshot(owner_path)
                    ctx.forge_snapshot = snap
                    if snap.repo is not None:
                        ctx.visibility = snap.repo.visibility
                        res.visibility = snap.repo.visibility
                        res.archived = snap.repo.archived
                        res.web_url = snap.repo.html_url or res.web_url
                        res.forge_info.update(
                            {
                                "default_branch": snap.repo.default_branch,
                                "ci": snap.ci.state if snap.ci else None,
                                "ci_number": snap.ci.number if snap.ci else None,
                                "protected": snap.protected,
                                "alerts": sum(snap.alerts.values()) if snap.alerts else None,
                                "open_items": len([i for i in snap.open_items if i.kind == "pr"]),
                            }
                        )
                    for e in snap.errors:
                        res.errors.setdefault("forge", e)
                    if "email-exposure" in ids and ctx.visibility == "public":
                        ctx.noreply_email = await forge.noreply_email()
                except HttpError as err:
                    ctx.forge_status = _("hosting {forge_name} nedostupný: {err}").format(
                        forge_name=ctx.forge_name, err=err
                    )
        elif not self.options.use_forges:
            ctx.forge_status = _("hostingy vypnuté (--no-forges)")
        host = res.forge_info.get("host")
        if (
            ctx.visibility == "unknown"
            and host == "github.com"
            and isinstance(owner_path, str)
            and net.github_public
        ):
            vis = await net.github_public.public_visibility(owner_path)
            if vis != "unknown":
                ctx.visibility = vis  # type: ignore[assignment]
                res.visibility = vis  # type: ignore[assignment]
        if "ci-security" in ids:
            await self._resolve_actions(ctx, net)
        if ids & {
            "deps-vulnerable",
            "deps-outdated",
            "deps-lockfile-missing",
            "deps-unmaintained",
            "license-compat",
        }:
            data: DepsData = await asyncio.to_thread(collect, ctx.tracked_files, ctx.read_text)
            ctx.deps = data
            if ids & {"deps-vulnerable", "deps-outdated", "deps-unmaintained", "license-compat"}:
                await enrich(
                    net.public,
                    data,
                    ttl=self.config.limits.registry_cache_hours * 3600,
                    want_vulns="deps-vulnerable" in ids,
                    want_outdated="deps-outdated" in ids,
                    want_licenses="license-compat" in ids,
                    want_registry="deps-unmaintained" in ids,
                )
                if data.errors and not data.vulns and any(e.startswith("OSV") for e in data.errors):
                    ctx.deps_status = _("OSV.dev nedostupné")
                    data.network_done = False

    async def _resolve_actions(self, ctx: RepoContext, net: NetServices) -> None:
        """SHA tagů nepřipnutých akcí z GitHubu (pro léčbu ci-security). Max. 30 na repo."""
        from repo_doctor.checks.ci_security import load_workflows, trusted_owners, unpinned_refs

        workflows = await asyncio.to_thread(load_workflows, ctx)
        refs = [r for r in unpinned_refs(workflows, trusted_owners(ctx)) if "://" not in r.uses]
        github = next(
            (
                f
                for f in net.forges.forges.values()
                if isinstance(f, GitHubForge) and not f.config.url
            ),
            net.github_public,
        )
        if not refs or github is None:
            return
        for key in sorted({r.key for r in refs})[:30]:
            repo_name, _, ref = key.rpartition("@")
            sha = await github.resolve_ref(repo_name, ref)
            if sha:
                ctx.action_shas[key] = sha

    async def _scan_repo(
        self, repo: DiscoveredRepo, now: datetime, total: int, done: Callable[[], int]
    ) -> RepoResult:
        started = time.monotonic()
        if await asyncio.to_thread(self._untrusted, repo):
            res = self._untrusted_result(repo)
            res.duration_ms = int((time.monotonic() - started) * 1000)
            return res
        ctx, res = await asyncio.to_thread(self._context, repo, now)
        await self._network(ctx, res)
        res.details = await asyncio.to_thread(self._details, ctx, now)
        findings = []
        disabled = set(ctx.config.checks.disabled)  # včetně .repo-doctor.toml
        for check in self.checks:
            if self._cancelled:
                break
            if check.id in disabled:
                continue
            self.on_event(
                ScanEvent("check", total=total, done=done(), repo=repo.name, check=check.id)
            )
            try:
                findings.extend(await asyncio.to_thread(check.run, ctx))
            except SkipCheck as skip:
                res.skipped[check.id] = str(skip)
            except (GitError, OSError, ValueError, HttpError) as err:
                res.errors[check.id] = redact(f"{type(err).__name__}: {err}")[:300]
        allowed = {a.hash for a in ctx.config.allowlist}
        kept = [f for f in findings if f.fingerprint(repo.name) not in allowed]
        res.allowlisted = len(findings) - len(kept)
        res.hidden_fakes = len(ctx.hidden_fakes)
        res.findings = kept
        res.score = score(kept)
        res.duration_ms = int((time.monotonic() - started) * 1000)
        return res


def aggregate_pulse(result: ScanResult) -> list[int]:
    total = [0] * 30
    for r in result.repos:
        for i, v in enumerate(r.state.pulse[:30]):
            total[i] += v
    return total
