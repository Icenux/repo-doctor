"""Odeslání oprav: push větve, PR/MR a merge (na hostingu i lokálně).

Navazuje na léčbu (`fixes`), která vytvoří větev `repo-doctor/fixes-<datum>`. Každý krok je
samostatný a má dvojici `plan_*` (nic nemění, slouží k náhledu) a `apply_*` (těsně před zápisem
si stav ověří znovu):

- `push`: pošle jen opravnou větev (nebo výchozí větev po lokálním merge), nikdy force ani tagy;
  předtím projde nové commity `guard` (tajemství) a hooky zůstávají vypnuté.
- `pr`: vytvoří PR/MR přes API hostingu (token z konfigurace hostingu, musí mít právo zápisu).
- `merge`: mergne PR na hostingu, ale jen když je CI zelená (nebo žádná není) a hosting hlásí
  `mergeable`; hosting dostane SHA hlavičky, takže merge selže, pokud se větev mezitím změnila.
- `merge-local`: jen fast-forward výchozí větve; pracovní strom se nemění, pokud výchozí větev
  není checkoutnutá. Nikdy merge commit, rebase ani reset.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from repo_doctor import czech
from repo_doctor.config import Config
from repo_doctor.fixes import BRANCH_PREFIX
from repo_doctor.forges import ForgeUnavailable, connect_forge
from repo_doctor.forges.base import (
    CIStatus,
    Forge,
    ForgeRepo,
    PullRequestInfo,
    pick_merge_method,
)
from repo_doctor.forges.urls import RemoteURL, SshConfig, match_forge, parse_remote
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.guard import render, run_guard
from repo_doctor.httpclient import AuthError, ForbiddenError, HttpError, NotFoundError
from repo_doctor.i18n import _
from repo_doctor.masking import redact, redact_url_credentials
from repo_doctor.pipeline import attach_needs
from repo_doctor.sshhelp import friendly_git_error

ZERO_SHA = "0" * 40


class PublishRefused(Exception):
    """Krok nelze bezpečně provést; zpráva vysvětluje proč a co dál."""


@dataclass
class CommitInfo:
    sha: str
    subject: str
    body: str = ""

    @property
    def short(self) -> str:
        return self.sha[:8]


# ---------------------------------------------------------------------------- společné


def _check_repo(git: Git) -> None:
    if git.dubious_ownership():
        raise PublishRefused(_("Repo patří jinému uživateli – repo-doctor ho nikdy nemění."))


def fix_branches(git: Git) -> list[str]:
    """Opravné větve repo-doctoru, nejnovější první."""
    out = git.run(
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)",
        f"refs/heads/{BRANCH_PREFIX}*",
        check=False,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def resolve_branch(git: Git, branch: str | None) -> str:
    """Opravná větev k odeslání: zadaná (jen `repo-doctor/fixes-*`), jinak nejnovější."""
    if branch is None:
        found = fix_branches(git)
        if not found:
            raise PublishRefused(
                _("Repo nemá žádnou opravnou větev {prefix}* – nejdřív proveď léčbu (f).").format(
                    prefix=BRANCH_PREFIX
                )
            )
        return found[0]
    if not branch.startswith(BRANCH_PREFIX) or not git.check_branch_name(branch):
        raise PublishRefused(
            _("Odeslat jde jen opravnou větev {prefix}*, ne {branch}.").format(
                prefix=BRANCH_PREFIX, branch=branch
            )
        )
    if git.rev_parse(f"refs/heads/{branch}") is None:
        raise PublishRefused(_("Větev {branch} neexistuje.").format(branch=branch))
    return branch


def default_branch(git: Git) -> str:
    name = git.default_branch()
    if name is None or git.rev_parse(f"refs/heads/{name}") is None:
        raise PublishRefused(_("Výchozí větev repa se nepodařilo určit."))
    return name


def pick_remote(git: Git, config: Config) -> str:
    remotes = git.remotes()
    if not remotes:
        raise PublishRefused(
            _("Repo nemá žádný remote – push a PR nejdou. Lokální merge funguje i bez něj.")
        )
    wanted = config.publish.remote
    if wanted:
        if wanted not in remotes:
            raise PublishRefused(
                _("Remote {remote} z nastavení [publish] v repu není.").format(remote=wanted)
            )
        return wanted
    default = git.default_branch()
    tracked = git.config_get(f"branch.{default}.remote") if default else None
    for candidate in (tracked, "origin"):
        if candidate in remotes:
            return candidate
    if len(remotes) == 1:
        return next(iter(remotes))
    raise PublishRefused(
        _("Repo má víc remotů ({names}) – vyber jeden v nastavení [publish] remote.").format(
            names=", ".join(sorted(remotes))
        )
    )


def _commit_infos(git: Git, *revs: str) -> list[CommitInfo]:
    return [CommitInfo(*c) for c in git.log_commits(*revs)]


# ---------------------------------------------------------------------------- push


@dataclass
class PushPlan:
    branch: str
    remote: str
    remote_url: str  # bez přihlašovacích údajů
    sha: str
    commits: list[CommitInfo] = field(default_factory=list)
    is_default: bool = False


def plan_push(
    git: Git, config: Config, branch: str | None = None, *, default: bool = False
) -> PushPlan:
    """Co se pošle. `default=True` = výchozí větev (po lokálním merge), jinak opravná větev."""
    _check_repo(git)
    if git.status().conflicted:
        raise PublishRefused(_("Repo má nevyřešené konflikty – nejdřív je vyřeš."))
    name = default_branch(git) if default else resolve_branch(git, branch)
    remote = pick_remote(git, config)
    sha = git.rev_parse(f"refs/heads/{name}")
    if sha is None:  # pragma: no cover - ošetřeno výše
        raise PublishRefused(_("Větev {branch} neexistuje.").format(branch=name))
    tracking = git.rev_parse(f"refs/remotes/{remote}/{name}")
    if tracking is not None and not git.is_ancestor(tracking, sha):
        raise PublishRefused(
            _(
                "Větev {branch} se na {remote} liší od lokální (není její předchůdce). Stáhni novinky (fetch) a srovnej ji ručně – repo-doctor nikdy nepřepisuje historii."
            ).format(branch=name, remote=remote)
        )
    commits = [CommitInfo(*c) for c in git.commits_not_on_remotes(f"refs/heads/{name}")]
    if not commits:
        raise PublishRefused(
            _("Není co poslat – všechny commity větve {branch} už na některém remote jsou.").format(
                branch=name
            )
        )
    result = run_guard(git.path, [f"refs/heads/{name} {sha} refs/heads/{name} {ZERO_SHA}"], config)
    if result.blocked:
        raise PublishRefused(render(result))
    url = redact_url_credentials((git.push_urls(remote) or [git.remotes()[remote]])[0])
    return PushPlan(name, remote, url, sha, commits, is_default=default)


def apply_push(git: Git, plan: PushPlan, config: Config) -> PushPlan:
    """Pošle větev. Stav se ověří znovu (větev se od náhledu nesmí změnit)."""
    fresh = plan_push(git, config, plan.branch, default=plan.is_default)
    if fresh.sha != plan.sha or fresh.remote != plan.remote:
        raise PublishRefused(_("Větev se od náhledu změnila – zkus to znovu."))
    try:
        git.push_branch(plan.remote, plan.branch)
    except GitError as err:
        raise PublishRefused(
            _("Push selhal: {why}").format(
                why=friendly_git_error(err.stderr, git.remotes().get(plan.remote))[:400]
            )
        ) from err
    return plan


# ---------------------------------------------------------------------------- hosting


@dataclass
class Hosting:
    """Hosting repa: klient s tokenem a údaje repa. Po použití `aclose()`."""

    forge: Forge
    repo: ForgeRepo
    remote: str
    owner_path: str

    async def aclose(self) -> None:
        await self.forge.client.aclose()


def describe_http_error(err: HttpError, action: str) -> str:
    """Srozumitelná zpráva pro chybu API hostingu (`action` = např. „vytvoření PR“)."""
    if isinstance(err, AuthError):
        return _("Hosting odmítl token (neplatný nebo expirovaný) při: {action}.").format(
            action=action
        )
    if isinstance(err, ForbiddenError):
        return _(
            "Token nemá právo zápisu při: {action}. Potřebuje Contents a Pull requests: read & write (GitLab scope api)."
        ).format(action=action)
    if isinstance(err, NotFoundError):
        return _(
            "Hosting vrátil 404 při: {action} – repo neexistuje nebo k němu token nemá přístup."
        ).format(action=action)
    if err.status in (405, 409, 422):
        return _("Hosting odmítl ({status}) při: {action} – {detail}").format(
            status=err.status, action=action, detail=str(err)
        )
    return _("{action}: {err}").format(action=action, err=err)


async def connect(
    git: Git,
    config: Config,
    remote: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    ssh_config: SshConfig | None = None,
) -> Hosting:
    """Najde hosting podle URL remotu a připojí se k němu tokenem z konfigurace."""
    url = git.remotes().get(remote)
    parsed: RemoteURL | None = parse_remote(url, ssh_config or SshConfig.load()) if url else None
    forge_config = match_forge(parsed, config.forges) if parsed else None
    if parsed is None or forge_config is None:
        raise PublishRefused(
            _(
                "Remote {remote} nepatří žádnému nastavenému hostingu – přidej ho na obrazovce Hostingy (2)."
            ).format(remote=remote)
        )
    try:
        forge = connect_forge(forge_config, transport=transport, ttl=0)
    except ForgeUnavailable as err:
        raise PublishRefused(
            _("Hosting {name} nejde použít: {why}").format(name=forge_config.name, why=err)
        ) from err
    if not forge.authenticated:
        await forge.client.aclose()
        raise PublishRefused(
            _("Hosting {name} nemá token – PR a merge vyžadují token s právem zápisu.").format(
                name=forge_config.name
            )
        )
    try:
        repo = await forge.get_repo(parsed.path)
    except HttpError as err:
        await forge.client.aclose()
        raise PublishRefused(describe_http_error(err, _("načtení repa"))) from err
    if repo.archived:
        await forge.client.aclose()
        raise PublishRefused(_("Repo je na hostingu archivované (jen pro čtení)."))
    return Hosting(forge, repo, remote, parsed.path)


async def with_hosting[T](
    git: Git,
    config: Config,
    work: Callable[[Hosting], Awaitable[T]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> T:
    """Připojí hosting remotu repa, spustí `work` a klienta vždy zavře."""
    hosting = await connect(git, config, pick_remote(git, config), transport=transport)
    try:
        return await work(hosting)
    finally:
        await hosting.aclose()


# ---------------------------------------------------------------------------- PR


@dataclass
class PRPlan:
    branch: str
    base: str
    title: str
    body: str
    forge: str
    owner_path: str
    commits: list[CommitInfo] = field(default_factory=list)


def pr_text(commits: list[CommitInfo]) -> tuple[str, str]:
    """Titulek a popis PR z commitů opravné větve (jen to, co repo-doctor samo napsal)."""
    if len(commits) == 1:
        title = commits[0].subject
    else:
        title = _("chore(repo-doctor): automatické opravy ({n})").format(n=len(commits))
    lines = [
        _("Automatické opravy z repo-doctor – {commits}:").format(
            commits=czech.commits(len(commits))
        ),
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.short}` {c.subject}")
        lines.extend(f"  {line}".rstrip() for line in c.body.splitlines())
    lines += ["", _("Vytvořeno nástrojem repo-doctor. Změny zkontroluj před mergem.")]
    return redact(title), redact("\n".join(lines))


async def plan_pr(git: Git, hosting: Hosting, config: Config, branch: str | None = None) -> PRPlan:
    _check_repo(git)
    name = resolve_branch(git, branch)
    sha = git.rev_parse(f"refs/heads/{name}")
    if git.rev_parse(f"refs/remotes/{hosting.remote}/{name}") != sha:
        raise PublishRefused(
            _(
                "Větev {branch} není na {remote} v aktuální podobě – nejdřív ji pošli (push)."
            ).format(branch=name, remote=hosting.remote)
        )
    base = hosting.repo.default_branch
    if not base:
        raise PublishRefused(_("Hosting nehlásí výchozí větev repa."))
    try:
        existing = await hosting.forge.find_pr(hosting.repo, name)
    except HttpError as err:
        raise PublishRefused(describe_http_error(err, _("hledání PR"))) from err
    if existing is not None and existing.state == "open":
        raise PublishRefused(
            _("PR už existuje: #{number} {url}").format(number=existing.number, url=existing.url)
        )
    if existing is not None and existing.state == "merged":
        raise PublishRefused(
            _("PR #{number} z této větve už je mergnutý: {url}").format(
                number=existing.number, url=existing.url
            )
        )
    commits = _new_commits(git, f"refs/remotes/{hosting.remote}/{base}", name)
    if not commits:
        raise PublishRefused(
            _("Větev {branch} nemá oproti {base} žádné nové commity.").format(
                branch=name, base=base
            )
        )
    title, body = pr_text(commits)
    return PRPlan(name, base, title, body, hosting.forge.name, hosting.owner_path, commits)


def _new_commits(git: Git, base_ref: str, branch: str) -> list[CommitInfo]:
    """Commity `branch`, které nejsou v `base_ref`; bez `base_ref` ty, které nejsou na žádném remote."""
    if git.rev_parse(base_ref) is None:
        return [CommitInfo(*c) for c in git.commits_not_on_remotes(f"refs/heads/{branch}")]
    return _commit_infos(git, f"{base_ref}..refs/heads/{branch}")


async def apply_pr(git: Git, hosting: Hosting, config: Config, plan: PRPlan) -> PullRequestInfo:
    fresh = await plan_pr(git, hosting, config, plan.branch)
    if fresh.base != plan.base:
        raise PublishRefused(_("Výchozí větev hostingu se od náhledu změnila – zkus to znovu."))
    try:
        return await hosting.forge.create_pr(
            hosting.repo, head=plan.branch, base=plan.base, title=plan.title, body=plan.body
        )
    except HttpError as err:
        raise PublishRefused(describe_http_error(err, _("vytvoření PR"))) from err


# ---------------------------------------------------------------------------- merge na hostingu


@dataclass
class MergePlan:
    pr: PullRequestInfo
    ci: CIStatus
    method: str | None
    blockers: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.blockers


def _ci_blocker(ci: CIStatus) -> str | None:
    if ci.state in ("success", "none"):
        return None
    if ci.state == "pending":
        return _("CI ještě běží – počkej, až doběhne.")
    if ci.state == "failure":
        return _("CI selhala – oprav ji, než PR mergneš.")
    return _("Stav CI nejde zjistit (token nemá právo ho číst) – merge se nenabízí.")


async def plan_merge(
    git: Git, hosting: Hosting, config: Config, branch: str | None = None
) -> MergePlan:
    _check_repo(git)
    name = resolve_branch(git, branch)
    forge, repo = hosting.forge, hosting.repo
    try:
        found = await forge.find_pr(repo, name)
        if found is None:
            raise PublishRefused(
                _("Pro větev {branch} není žádný PR – nejdřív ho vytvoř.").format(branch=name)
            )
        pr = await forge.get_pr(repo, found.number)  # seznam nemá `mergeable`
        ci = await forge.pr_ci(repo, pr)
    except HttpError as err:
        raise PublishRefused(describe_http_error(err, _("načtení PR"))) from err
    if pr.state == "merged":
        raise PublishRefused(
            _("PR #{number} už je mergnutý: {url}").format(number=pr.number, url=pr.url)
        )
    if pr.state == "closed":
        raise PublishRefused(
            _("PR #{number} je zavřený: {url}").format(number=pr.number, url=pr.url)
        )
    return MergePlan(
        pr,
        ci,
        pick_merge_method(repo, config.publish.merge_method),
        _merge_blockers(pr, ci, git.rev_parse(f"refs/heads/{name}")),
    )


def _merge_blockers(pr: PullRequestInfo, ci: CIStatus, local_sha: str | None) -> list[str]:
    """Co brání mergi PR (prázdné = jde mergnout)."""
    blockers: list[str] = []
    if local_sha and pr.head_sha and local_sha != pr.head_sha:
        blockers.append(
            _(
                "Větev na hostingu se liší od lokální – srovnej je (push nebo fetch) a zkus to znovu."
            )
        )
    if pr.mergeable is False:
        blockers.append(_("PR nejde mergnout (konflikt, ochrana větve nebo chybějící schválení)."))
    elif pr.mergeable is None:
        blockers.append(_("Hosting ještě zjišťuje, jestli jde PR mergnout – zkus to za chvíli."))
    if blocker := _ci_blocker(ci):
        blockers.append(blocker)
    return blockers


async def apply_merge(
    git: Git, hosting: Hosting, config: Config, plan: MergePlan
) -> PullRequestInfo:
    """Mergne PR. Stav (včetně CI) se načte znovu, a pokud něco brání, nemerguje."""
    fresh = await plan_merge(git, hosting, config, plan.pr.head_branch or None)
    if fresh.pr.number != plan.pr.number or fresh.pr.head_sha != plan.pr.head_sha:
        raise PublishRefused(_("PR se od náhledu změnil – zkus to znovu."))
    if not fresh.ready:
        raise PublishRefused("\n".join(fresh.blockers))
    try:
        await hosting.forge.merge_pr(hosting.repo, fresh.pr, fresh.method)
        return await hosting.forge.get_pr(hosting.repo, fresh.pr.number)
    except HttpError as err:
        raise PublishRefused(describe_http_error(err, _("merge PR"))) from err


# ---------------------------------------------------------------------------- stav (stránka Odeslání)


@dataclass
class PublishStatus:
    """Kde je oprava na cestě ven – pro stránku Odeslání. Nic nemění a nic nevyžaduje.

    Místní část (`branch`…`default_ahead`) se zjistí vždy; část z hostingu (`pr`, `ci`,
    `blockers`) jen když se k němu jde připojit – jinak je v `hosting_note` proč ne.
    """

    branch: str | None = None
    default: str | None = None
    remote: str | None = None
    commits: list[CommitInfo] = field(default_factory=list)  # opravná větev oproti výchozí
    pushed: bool | None = None  # opravná větev je na remote ve stejné podobě
    can_merge_local: bool = False
    default_ahead: int = 0  # commity výchozí větev, které nejsou na žádném remote
    pr: PullRequestInfo | None = None
    ci: CIStatus | None = None
    blockers: list[str] = field(default_factory=list)
    hosting_note: str = ""
    local_note: str = ""  # proč chybí místní část (např. žádná opravná větev)

    @property
    def can_merge(self) -> bool:
        return self.pr is not None and self.pr.state == "open" and not self.blockers


def local_status(git: Git, config: Config) -> PublishStatus:
    """Místní část stavu: opravná větev, jestli je na remote, a co jde mergnout/poslat."""
    st = PublishStatus()
    try:
        _check_repo(git)
        st.default = default_branch(git)
    except PublishRefused as err:
        st.local_note = str(err)
        return st
    try:
        st.remote = pick_remote(git, config)
    except PublishRefused as err:
        st.hosting_note = str(err)
    try:
        st.default_ahead = len(git.commits_not_on_remotes(f"refs/heads/{st.default}"))
    except GitError:  # pragma: no cover - repo, které git nepřečte
        st.default_ahead = 0
    found = fix_branches(git)
    if not found:
        st.local_note = _(
            "Repo nemá žádnou opravnou větev {prefix}* – nejdřív proveď léčbu (f)."
        ).format(prefix=BRANCH_PREFIX)
        return st
    st.branch = found[0]
    sha = git.rev_parse(f"refs/heads/{st.branch}")
    base = f"refs/remotes/{st.remote}/{st.default}" if st.remote else ""
    st.commits = _new_commits(git, base, st.branch) if base else _new_commits(git, "", st.branch)
    if st.remote:
        st.pushed = git.rev_parse(f"refs/remotes/{st.remote}/{st.branch}") == sha
    try:
        plan_merge_local(git, config, st.branch)
        st.can_merge_local = True
    except PublishRefused:
        st.can_merge_local = False
    return st


async def hosting_status(git: Git, hosting: Hosting, status: PublishStatus) -> None:
    """Doplní do `status` PR, CI (i po jednotlivých kontrolách) a překážky mergi z hostingu."""
    if status.branch is None:
        return
    forge, repo = hosting.forge, hosting.repo
    try:
        found = await forge.find_pr(repo, status.branch)
        if found is None:
            return
        pr = await forge.get_pr(repo, found.number)
        ci = await forge.pr_ci(repo, pr)
    except HttpError as err:
        status.hosting_note = describe_http_error(err, _("načtení PR"))
        return
    status.pr, status.ci = pr, ci
    if ci.jobs and pr.head_sha:
        sha = pr.head_sha  # závislosti jobů (`needs`) API nevrací, čtou se z workflow na commitu PR

        def read(path: str) -> str | None:
            blob = git.show_file(sha, path)
            return blob.decode("utf-8", "replace") if blob is not None else None

        try:
            attach_needs(ci.jobs, read)
        except (GitError, OSError, RecursionError, ValueError, TypeError, MemoryError):
            for job in ci.jobs:  # volitelné obohacení; při chybě zůstane plochý seznam
                job.needs = []
    if pr.state == "open":
        status.blockers = _merge_blockers(pr, ci, git.rev_parse(f"refs/heads/{status.branch}"))


async def publish_status(
    git: Git,
    config: Config,
    *,
    online: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PublishStatus:
    """Celý stav; hosting se čte jen s `online` a když má repo token – jinak je to v poznámce."""
    status = local_status(git, config)
    if status.branch is None or status.remote is None:
        return status
    if not online:
        status.hosting_note = _("Offline režim – PR a CI se nenačítají.")
        return status
    try:
        hosting = await connect(git, config, status.remote, transport=transport)
    except PublishRefused as err:
        status.hosting_note = str(err)
        return status
    try:
        await hosting_status(git, hosting, status)
    finally:
        await hosting.aclose()
    return status


# ---------------------------------------------------------------------------- lokální merge


@dataclass
class LocalMergePlan:
    branch: str
    target: str
    old: str  # SHA výchozí větve před mergem
    new: str  # SHA opravné větve
    commits: list[CommitInfo] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    checked_out: bool = False  # výchozí větev je v tomto pracovním stromě → změní se soubory


def plan_merge_local(git: Git, config: Config, branch: str | None = None) -> LocalMergePlan:
    _check_repo(git)
    name = resolve_branch(git, branch)
    target = default_branch(git)
    if name == target:
        raise PublishRefused(_("Opravná větev je zároveň výchozí – není co mergovat."))
    old = git.rev_parse(f"refs/heads/{target}")
    new = git.rev_parse(f"refs/heads/{name}")
    if old is None or new is None:  # pragma: no cover - ošetřeno výše
        raise PublishRefused(_("Větev neexistuje."))
    if old == new or git.is_ancestor(new, old):
        raise PublishRefused(
            _("Větev {branch} už ve {target} je – není co mergovat.").format(
                branch=name, target=target
            )
        )
    if not git.is_ancestor(old, new):
        raise PublishRefused(
            _(
                "Výchozí větev {target} mezitím povyrostla (historie se rozdělila). repo-doctor dělá jen fast-forward – větev {branch} si srovnej ručně (rebase nebo merge)."
            ).format(target=target, branch=name)
        )
    other = git.worktree_branches().get(target)
    here = git.toplevel()
    checked_out = other is not None and here is not None and Path(other) == here
    if other is not None and not checked_out:
        raise PublishRefused(
            _("Větev {target} je checkoutnutá v jiném pracovním stromě ({path}).").format(
                target=target, path=other
            )
        )
    files = [f for f in git.run("diff", "--name-only", "-z", old, new, "--").split("\0") if f]
    if checked_out:
        _check_worktree(git, files)
    commits = _commit_infos(git, f"refs/heads/{target}..refs/heads/{name}")
    return LocalMergePlan(name, target, old, new, commits, files, checked_out)


def _check_worktree(git: Git, files: list[str]) -> None:
    """Fast-forward změní soubory – smí to jen v čistém pracovním stromě."""
    from repo_doctor.sync import overwrite_clash

    st = git.status()
    tracked = st.staged + st.modified + st.conflicted  # nesledované řeší `overwrite_clash`
    if tracked:
        raise PublishRefused(
            _(
                "Repo má necommitnuté změny ({dirty} souborů) a výchozí větev je právě checkoutnutá – commitni je nebo ulož do stashe."
            ).format(dirty=tracked)
        )
    if not git.worktree_is_path():
        raise PublishRefused(
            _("Pracovní strom repa je jinde než repo (core.worktree) – nic se nemění.")
        )
    if any(k.startswith(("filter.", "merge.")) for k, _v in git.hardening_pairs()):
        raise PublishRefused(
            _("Repo má vlastní filtry/drivery v .git/config – mergni ručně (git merge --ff-only).")
        )
    clash = overwrite_clash(git, files)
    if clash:
        raise PublishRefused(
            _("Nesledované nebo ignorované soubory by se přepsaly: {files}").format(
                files=", ".join(clash[:3])
            )
        )


def apply_merge_local(git: Git, config: Config, plan: LocalMergePlan) -> LocalMergePlan:
    fresh = plan_merge_local(git, config, plan.branch)
    if (fresh.old, fresh.new) != (plan.old, plan.new):
        raise PublishRefused(_("Větve se od náhledu změnily – zkus to znovu."))
    try:
        if plan.checked_out:
            git.run("merge", "--ff-only", "--no-edit", "--quiet", plan.new)
        else:
            git.move_branch(plan.target, plan.new, plan.old)
    except GitError as err:
        raise PublishRefused(
            _("Merge selhal: {why}").format(why=(err.stderr.strip() or str(err))[:300])
        ) from err
    return plan
