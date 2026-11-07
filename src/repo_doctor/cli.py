"""Neinteraktivní CLI (pro CI a skripty) a vstupní bod `repo-doctor`.

Bez argumentů (nebo jen s cestami) na TTY spustí TUI; bez TTY vypíše nápovědu.
CLI mění repa jen na výslovný pokyn (`--apply`, výchozí je náhled): úklid, synchronizace
a odeslání oprav (`publish`). Léčba (tvorba opravné větve) jde jen přes TUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer

from repo_doctor import __version__, czech, i18n, netpolicy, paths, publish
from repo_doctor.checkdocs import load as load_doc
from repo_doctor.checks import all_checks, check_ids
from repo_doctor.config import Config, ConfigError, ConfigStore, RootConfig
from repo_doctor.gitwrap import Git, configure_ssh
from repo_doctor.i18n import _
from repo_doctor.masking import install_log_redaction, redact
from repo_doctor.models import ScanResult, Severity
from repo_doctor.publish import PublishRefused
from repo_doctor.reports import FORMATS, ReportFormat, render

app = typer.Typer(
    name="repo-doctor",
    help=_(
        "Audit a údržba git repozitářů. Bez příkazu spustí TUI (jen na terminálu). "
        "Jazyk: --lang cs|en (jinak podle systému)."
    ),
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
forges_app = typer.Typer(help=_("Připojené git hostingy."), no_args_is_help=True)
config_app = typer.Typer(help=_("Konfigurace."), no_args_is_help=True)
app.add_typer(forges_app, name="forges")
app.add_typer(config_app, name="config")
publish_app = typer.Typer(
    help=_(
        "Odeslání oprav: push větve, PR/MR a merge. Výchozí je náhled, --apply opravdu provede."
    ),
    no_args_is_help=True,
)
app.add_typer(publish_app, name="publish")
schedule_app = typer.Typer(
    help=_("Plánovaný sken (uživatelský systemd timer)."), no_args_is_help=True
)
app.add_typer(schedule_app, name="schedule")


def _err(message: str) -> None:
    typer.echo(redact(message), err=True)


def _load_config() -> Config:
    try:
        config = ConfigStore().load()
    except ConfigError as err:
        _err(str(err))
        raise typer.Exit(2) from None
    configure_ssh(batch_mode=config.ssh_batch_mode)
    netpolicy.configure(config.allow_http_hosts)
    return config


def _split(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    out = [v.strip() for item in values for v in item.split(",") if v.strip()]
    unknown = sorted(set(out) - set(check_ids()))
    if unknown:
        _err(
            _("Neznámé kontroly: {join}. Seznam: repo-doctor checks").format(
                join=", ".join(unknown)
            )
        )
        raise typer.Exit(2)
    return out


def exit_code(result: ScanResult, fail_on: Severity) -> int:
    return (
        1 if any(f.severity.rank >= fail_on.rank for r in result.repos for f in r.findings) else 0
    )


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help=_("Vypíše verzi."))] = False,
) -> None:
    if version:
        typer.echo(f"repo-doctor {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command(
    help=_(
        "Naskenuje repozitáře a vypíše report. Exit: 0 bez nálezů nad prahem, 1 nálezy, 2 chyba.\n\nS --scheduled se report nevypisuje (--report/--output nejdou kombinovat) a exit je 0."
    )
)
def scan(
    path: Annotated[
        list[Path] | None,
        typer.Argument(help=_("Složky ke skenu (bez nich složky z konfigurace).")),
    ] = None,
    report: Annotated[
        str, typer.Option("--report", "-r", help=_("Formát: md | json | html."))
    ] = "md",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help=_("Soubor pro report (jinak stdout)."))
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help=_("Přeskočí všechny síťové kontroly."))
    ] = False,
    no_forges: Annotated[
        bool, typer.Option("--no-forges", help=_("Nepřipojovat se k hostingům."))
    ] = False,
    only: Annotated[
        list[str] | None, typer.Option("--only", help=_("Jen tyto kontroly (čárkami)."))
    ] = None,
    skip: Annotated[
        list[str] | None, typer.Option("--skip", help=_("Vynechat kontroly (čárkami)."))
    ] = None,
    jobs: Annotated[
        int | None, typer.Option("--jobs", "-j", min=1, help=_("Počet paralelně skenovaných rep."))
    ] = None,
    fail_on: Annotated[
        str | None, typer.Option("--fail-on", help=_("Práh exit kódu: high | medium | low."))
    ] = None,
    depth: Annotated[
        int, typer.Option("--depth", min=0, max=12, help=_("Hloubka pro zadané cesty."))
    ] = 3,
    fetch: Annotated[
        bool,
        typer.Option("--fetch", help=_("Před skenem `git fetch` (mění jen remote-tracking refy).")),
    ] = False,
    scheduled: Annotated[
        bool,
        typer.Option(
            "--scheduled",
            help=_(
                "Plánovaný běh: uloží výsledek, porovná s minulým a upozorní jen na změny. "
                "Nevypisuje report a končí 0 (kromě chyby)."
            ),
        ),
    ] = False,
) -> None:
    from repo_doctor.scanner import ScanEvent, Scanner, ScanOptions

    if scheduled and (output is not None or report != "md"):
        _err("--scheduled nejde kombinovat s --report ani --output.")
        raise typer.Exit(2)
    if report not in FORMATS:
        _err(
            _("Neznámý formát reportu {report!r} (povolené: {join}).").format(
                report=report, join=", ".join(FORMATS)
            )
        )
        raise typer.Exit(2)
    config = _load_config()
    try:
        threshold = Severity.parse(fail_on) if fail_on else config.checks.fail_on
    except ValueError:
        _err(
            _("Neplatný práh --fail-on {fail_on!r} (high | medium | low).").format(fail_on=fail_on)
        )
        raise typer.Exit(2) from None
    roots = [RootConfig(path=str(p), depth=depth) for p in path] if path else config.roots
    if not roots:
        _err(
            _(
                "Nejsou nastavené žádné složky. Zadej cestu nebo spusť TUI (průvodce). Konfigurace: {config_file}"
            ).format(config_file=paths.config_file())
        )
        raise typer.Exit(2)
    options = ScanOptions(
        offline=offline,
        use_forges=not no_forges,
        only=_split(only),
        skip=_split(skip),
        fetch=fetch or (scheduled and config.schedule.fetch),
    )
    if jobs:
        options.jobs = jobs
    interactive = sys.stderr.isatty()

    def progress(e: ScanEvent) -> None:
        if e.kind == "warning" and e.message:
            _err(_("varování: {message}").format(message=e.message))
        elif interactive and e.kind == "repo_done":
            typer.echo(f"\r[{e.done}/{e.total}] {e.repo or ''}".ljust(60), err=True, nl=False)

    try:
        result = asyncio.run(Scanner(config, options, on_event=progress).run(roots))
    except KeyboardInterrupt:  # pragma: no cover - interaktivní
        _err(_("Přerušeno."))
        raise typer.Exit(2) from None
    if interactive:
        typer.echo("", err=True)
    if scheduled:
        _scheduled_finish(result, config)
        return
    for r in result.repos:
        if "fetch" in r.errors:
            _err(f"fetch {r.name}: {r.errors['fetch']}")
    fmt: ReportFormat = report
    text = render(result, fmt, no_pulse_days=config.limits.no_pulse_days)
    if output:
        try:
            output.write_text(text, "utf-8")
        except OSError as err:
            _err(
                _("Report nelze zapsat do {output}: {strerror}").format(
                    output=output, strerror=err.strerror
                )
            )
            raise typer.Exit(2) from None
        _err(_("Report uložen: {output}").format(output=output))
    else:
        typer.echo(text, nl=False)
    raise typer.Exit(exit_code(result, threshold))


def _scheduled_finish(result: ScanResult, config: Config) -> None:
    """Uloží výsledek do historie, porovná s minulým a upozorní jen na změny."""
    from repo_doctor import history, notify

    previous = history.load_last_scan()
    if result.cancelled:
        return
    history.save_last_scan(result)
    history.record(
        result, keep=config.limits.history_size, no_pulse_days=config.limits.no_pulse_days
    )
    changes = notify.diff(previous, result)
    deliveries = notify.deliver(config.notify, changes.text()) if changes else []
    notify.save_state(result, changes, deliveries)
    if changes:
        typer.echo(_("Změny: {text}").format(text=changes.text()))
        for d in deliveries:
            typer.echo(f"  {'✓' if d.ok else '✗'} {d.channel} {d.detail}".rstrip())
    else:
        typer.echo(_("Beze změn – žádné upozornění."))


@app.command(help=_("Vysvětlí kontrolu a vypíše ruční postup opravy."))
def explain(
    check_id: Annotated[str, typer.Argument(help=_("ID kontroly, např. secrets-history."))],
) -> None:
    doc = load_doc(check_id)
    if doc is None:
        _err(
            _("Neznámá kontrola {check_id!r}. Seznam: repo-doctor checks").format(check_id=check_id)
        )
        raise typer.Exit(2)
    typer.echo(doc.text, nl=False)


@app.command(
    "clean",
    help=_(
        "Úklid místa: regenerovatelné složky ignorované gitem (node_modules, .venv, target…).\n\nVýchozí je náhled. S --apply se složky přesunou do koše (freedesktop Trash) – nikdy\ntrvale, nikdy nic sledovaného gitem ani mimo repo, nikdy repo cizího vlastníka.\n\nExit: 0 = hotovo, 1 = něco se přeskočilo, 2 = chyba zadání."
    ),
)
def clean_cmd(
    path: Annotated[
        list[Path] | None, typer.Argument(help=_("Složky s repy (bez nich složky z konfigurace)."))
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply", help=_("Náhled (výchozí), nebo opravdu přesunout do koše.")
        ),
    ] = True,
    all_repos: Annotated[
        bool,
        typer.Option("--all", help=_("I aktivní repa (jinak jen neaktivní podle konfigurace).")),
    ] = False,
    include_dirty: Annotated[
        bool, typer.Option("--include-dirty", help=_("I repa s necommitnutými změnami."))
    ] = False,
    duplicates: Annotated[
        bool,
        typer.Option(
            "--duplicates", help=_("I celé duplicitní klony bez změn (z posledního skenu).")
        ),
    ] = False,
    depth: Annotated[
        int,
        typer.Option("--depth", min=0, max=12, help=_("Hloubka hledání rep v zadaných složkách.")),
    ] = 3,
) -> None:
    from repo_doctor import history
    from repo_doctor.cleanup import (
        apply_cleanup,
        duplicate_candidates,
        human,
        inspect_repo,
        is_inactive,
        mark_busy,
    )
    from repo_doctor.discovery import discover

    config = _load_config()
    roots = _cli_roots(path, depth, config)
    chosen = []
    active, active_size = 0, 0
    for repo in discover(roots, config.ignore_repos).repos:
        info = inspect_repo(repo.path, config.cleanup.dirs)
        if info.error or not info.candidates:
            continue
        if not all_repos and not is_inactive(info, config.cleanup.inactive_days):
            active, active_size = active + 1, active_size + info.size
            continue
        mark_busy(info.candidates)
        note = (
            _("  (necommitnuté {dirty} – přeskočeno, viz --include-dirty)").format(dirty=info.dirty)
            if info.dirty and not include_dirty
            else ""
        )
        typer.echo(f"{repo.display}  {human(info.size)}{note}")
        for c in info.candidates:
            flag = _("  [běží proces – přeskočeno]") if c.busy else ""
            typer.echo(f"    {c.rel:<30} {human(c.size):>10}{flag}")
        if not info.dirty or include_dirty:
            chosen += [c for c in info.candidates if not c.busy]
    if duplicates:
        for c in duplicate_candidates(history.load_last_scan()):
            typer.echo(
                _("{path}  {human}  (celý duplicitní klon)").format(
                    path=c.path, human=human(c.size)
                )
            )
            chosen.append(c)
    total = sum(c.size for c in chosen)
    if active:
        typer.echo(
            _("\n{repos} s nedávnou aktivitou přeskočeno ({human}) – zahrne je --all.").format(
                repos=czech.repos(active), human=human(active_size)
            )
        )
    if not chosen:
        typer.echo(_("Nic k úklidu."))
        return
    if dry_run:
        typer.echo(
            _("\nCelkem k uvolnění: {human}. Nic se nesmazalo – použij --apply.").format(
                human=human(total)
            )
        )
        return
    result = apply_cleanup(chosen)
    for p, reason in result.skipped:
        typer.echo(_("  ! přeskočeno {p}: {reason}").format(p=p, reason=reason))
    typer.echo(
        _("Přesunuto do koše: {items}, uvolněno {human}.").format(
            items=czech.items(len(result.removed)), human=human(result.freed)
        )
    )
    if result.skipped:
        raise typer.Exit(1)


def _jde(n: int) -> str:
    return czech.plural(n, _("jde"), _("jdou"), _("jde"))


def _cli_roots(path: list[Path] | None, depth: int, config: Config) -> list[RootConfig]:
    if path:
        missing = [p for p in path if not p.expanduser().is_dir()]
        if missing:
            _err(_("Složka {v} neexistuje.").format(v=missing[0]))
            raise typer.Exit(2)
        return [RootConfig(path=str(p), depth=depth) for p in path]
    if not config.roots:
        _err(_("Nejsou nastavené žádné složky. Zadej cestu."))
        raise typer.Exit(2)
    return config.roots


@app.command(
    "sync",
    help=_(
        "Stav rep vůči remotu a „rychle dohnat“ – jen `git merge --ff-only`.\n\nJen čistá repa na sledované větvi, která jsou pozadu (ne rozdělená). Nikdy merge commit,\nrebase, reset ani push. Výchozí je náhled bez sítě; --fetch stáhne novinky, --apply dožene.\n\nExit: 0 = hotovo, 1 = fetch selhal nebo se něco přeskočilo, 2 = chyba zadání."
    ),
)
def sync_cmd(
    path: Annotated[
        list[Path] | None, typer.Argument(help=_("Složky s repy (bez nich složky z konfigurace)."))
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply", help=_("Náhled (výchozí), nebo opravdu dohnat fast-forwardem.")
        ),
    ] = True,
    fetch: Annotated[
        bool,
        typer.Option(
            "--fetch/--no-fetch",
            help=_("Nejdřív git fetch (aktualizuje jen remote-tracking refy; výchozí ne)."),
        ),
    ] = False,
    depth: Annotated[
        int,
        typer.Option("--depth", min=0, max=12, help=_("Hloubka hledání rep v zadaných složkách.")),
    ] = 3,
) -> None:
    from repo_doctor.discovery import discover
    from repo_doctor.sync import STATE_LABELS, fast_forward, status

    config = _load_config()
    roots = _cli_roots(path, depth, config)
    ready = []
    fetch_failed = False
    for repo in discover(roots, config.ignore_repos).repos:
        st = status(repo.path, fetch=fetch)
        line = f"{repo.display}  {_(STATE_LABELS[st.state])}"
        if st.behind or st.ahead:
            line += f" (↓{st.behind} ↑{st.ahead})"
        if st.can_fast_forward:
            line += _(" → dožene {commits}, změní {files}").format(
                commits=czech.commits(st.behind), files=czech.files(len(st.files))
            )
            ready.append(repo.path)
        elif st.reason and st.state not in ("clean",):
            line += f" · {st.reason}"
        if st.fetch_error:
            line += _(" · fetch: {fetch_error}").format(fetch_error=st.fetch_error)
            fetch_failed = True
        typer.echo(line)
    if dry_run:
        typer.echo(
            _("\nRychle dohnat {jde} {repos}. Nic se nezměnilo").format(
                jde=_jde(len(ready)), repos=czech.repos(len(ready))
            )
            + (_(" – použij --apply.") if ready else ".")
            + ("" if fetch else _(" Bez --fetch se porovnává s naposledy staženým stavem."))
        )
        if fetch_failed:
            raise typer.Exit(1)
        return
    result = fast_forward(ready)
    for p, reason in result.skipped:
        typer.echo(f"  ! {p}: {reason}")
    typer.echo(
        _("Dohnáno fast-forwardem: {repos}. Nic se nepushnulo.").format(
            repos=czech.repos(len(result.updated))
        )
    )
    if result.skipped or fetch_failed:
        raise typer.Exit(1)


# ----------------------------------------------------------------------------- publish
RepoArg = Annotated[Path, typer.Argument(help=_("Složka repa (výchozí aktuální)."))]
BranchOpt = Annotated[
    str | None,
    typer.Option("--branch", help=_("Opravná větev (výchozí nejnovější repo-doctor/fixes-*).")),
]
ApplyOpt = Annotated[
    bool,
    typer.Option("--dry-run/--apply", help=_("Náhled (výchozí), nebo opravdu provést.")),
]


def _publish_git(path: Path) -> Git:
    git = Git(paths.expand_path(str(path)))
    if not git.is_repo():
        _err(_("{path} není git repozitář.").format(path=path))
        raise typer.Exit(2)
    return Git(git.toplevel() or git.path)


def _publish_exit(err: PublishRefused) -> typer.Exit:
    _err(str(err))
    return typer.Exit(1)


def _echo_commits(commits: list[publish.CommitInfo]) -> None:
    for c in commits:
        typer.echo(f"  {c.short} {c.subject}")


@publish_app.command(
    "push",
    help=_(
        "Pošle opravnou větev na remote (bez force a tagů; před tím kontrola tajemství).\n\n"
        "S --default pošle výchozí větev po lokálním merge. Exit: 0 = hotovo, 1 = nelze, 2 = chyba zadání."
    ),
)
def publish_push(
    path: RepoArg = Path(),
    branch: BranchOpt = None,
    dry_run: ApplyOpt = True,
    default: Annotated[
        bool, typer.Option("--default", help=_("Poslat výchozí větev (po lokálním merge)."))
    ] = False,
) -> None:
    config = _load_config()
    git = _publish_git(path)
    try:
        plan = publish.plan_push(git, config, branch, default=default)
        typer.echo(
            _("Push {branch} → {remote} ({url}), {commits}:").format(
                branch=plan.branch,
                remote=plan.remote,
                url=plan.remote_url,
                commits=czech.commits(len(plan.commits)),
            )
        )
        _echo_commits(plan.commits)
        if dry_run:
            typer.echo(_("\nNic se neodeslalo – použij --apply."))
            return
        publish.apply_push(git, plan, config)
    except PublishRefused as err:
        raise _publish_exit(err) from None
    typer.echo(_("Odesláno: {branch} → {remote}.").format(branch=plan.branch, remote=plan.remote))


def _with_hosting(
    path: Path, work: Callable[[Git, publish.Hosting, Config], Awaitable[None]]
) -> None:
    config = _load_config()
    git = _publish_git(path)

    async def run(hosting: publish.Hosting) -> None:
        await work(git, hosting, config)

    try:
        asyncio.run(publish.with_hosting(git, config, run))
    except PublishRefused as err:
        raise _publish_exit(err) from None


@publish_app.command(
    "pr",
    help=_(
        "Vytvoří PR/MR z opravné větve (větev musí být na remote – nejdřív `publish push`).\n\n"
        "Token hostingu musí mít právo zápisu. Exit: 0 = hotovo, 1 = nelze, 2 = chyba zadání."
    ),
)
def publish_pr(path: RepoArg = Path(), branch: BranchOpt = None, dry_run: ApplyOpt = True) -> None:
    async def work(git: Git, hosting: publish.Hosting, config: Config) -> None:
        plan = await publish.plan_pr(git, hosting, config, branch)
        typer.echo(
            _("PR na {forge} ({owner_path}): {head} → {base}").format(
                forge=plan.forge, owner_path=plan.owner_path, head=plan.branch, base=plan.base
            )
        )
        typer.echo(f"\n{plan.title}\n\n{plan.body}")
        if dry_run:
            typer.echo(_("\nNic se nevytvořilo – použij --apply."))
            return
        pr = await publish.apply_pr(git, hosting, config, plan)
        typer.echo(_("Vytvořen PR #{number}: {url}").format(number=pr.number, url=pr.url))

    _with_hosting(path, work)


@publish_app.command(
    "merge",
    help=_(
        "Mergne PR na hostingu – jen když je CI zelená (nebo žádná není) a jde mergnout.\n\n"
        "Metodu merge určuje [publish] merge_method a nastavení repa. Exit: 0 = hotovo, 1 = nelze, 2 = chyba zadání."
    ),
)
def publish_merge(
    path: RepoArg = Path(), branch: BranchOpt = None, dry_run: ApplyOpt = True
) -> None:
    async def work(git: Git, hosting: publish.Hosting, config: Config) -> None:
        plan = await publish.plan_merge(git, hosting, config, branch)
        typer.echo(
            _("PR #{number} {title} ({url})\nCI: {ci} · metoda: {method}").format(
                number=plan.pr.number,
                title=plan.pr.title,
                url=plan.pr.url,
                ci=plan.ci.state,
                method=plan.method or _("výchozí hostingu"),
            )
        )
        if not plan.ready:
            for blocker in plan.blockers:
                typer.echo(f"  ✗ {blocker}")
            raise typer.Exit(1)
        if dry_run:
            typer.echo(_("\nNic se nemergnulo – použij --apply."))
            return
        done = await publish.apply_merge(git, hosting, config, plan)
        typer.echo(
            _("PR #{number} mergnut. Změny stáhneš: repo-doctor sync --fetch --apply").format(
                number=done.number
            )
            if done.state == "merged"
            else _("Hosting merge přijal, PR zatím není mergnutý: {url}").format(url=done.url)
        )

    _with_hosting(path, work)


@publish_app.command(
    "merge-local",
    help=_(
        "Lokálně posune výchozí větev na opravnou (jen fast-forward, bez merge commitu).\n\n"
        "Funguje i bez remote; pracovní strom se mění jen když je výchozí větev checkoutnutá a repo čisté. "
        "Exit: 0 = hotovo, 1 = nelze, 2 = chyba zadání."
    ),
)
def publish_merge_local(
    path: RepoArg = Path(), branch: BranchOpt = None, dry_run: ApplyOpt = True
) -> None:
    config = _load_config()
    git = _publish_git(path)
    try:
        plan = publish.plan_merge_local(git, config, branch)
        typer.echo(
            _("Merge {branch} → {target} (fast-forward), {commits}, změní {files}:").format(
                branch=plan.branch,
                target=plan.target,
                commits=czech.commits(len(plan.commits)),
                files=czech.files(len(plan.files)),
            )
        )
        _echo_commits(plan.commits)
        if not plan.checked_out:
            typer.echo(
                _("Pracovní strom se nezmění (větev {target} není checkoutnutá).").format(
                    target=plan.target
                )
            )
        if dry_run:
            typer.echo(_("\nNic se nezměnilo – použij --apply."))
            return
        publish.apply_merge_local(git, config, plan)
    except PublishRefused as err:
        raise _publish_exit(err) from None
    typer.echo(
        _("{target} posunuta na {branch}. Nic se nepushnulo.").format(
            target=plan.target, branch=plan.branch
        )
    )


@app.command(
    "guard",
    help=_(
        "Kontrola tajemství v commitech k pushi – pro hook pre-push (rozsah čte ze stdin).\n\nExit 0 = v pořádku, 1 = nalezená tajemství (push se zastaví). Výpis je maskovaný."
    ),
)
def guard_cmd(
    remote: Annotated[str | None, typer.Argument(help=_("Název remotu (předává git)."))] = None,
    url: Annotated[str | None, typer.Argument(help=_("URL remotu (předává git)."))] = None,
) -> None:
    from repo_doctor.guard import render, run_guard

    repo = Path.cwd()
    lines = [] if sys.stdin.isatty() else sys.stdin.read().splitlines()
    try:
        config = _load_config()
    except typer.Exit:
        from repo_doctor.config import Config

        config = Config()
    result = run_guard(repo, lines, config)
    if result.blocked:
        typer.echo(render(result), err=True)
        raise typer.Exit(1)


@app.command(
    "sbom", help=_("SBOM repozitáře (CycloneDX 1.5) z lockfilů. Bez sítě – licence jen z cache.")
)
def sbom_cmd(
    repo: Annotated[Path, typer.Argument(help=_("Cesta k repozitáři."))],
    fmt: Annotated[
        str, typer.Option("--format", help=_("Formát (zatím jen cyclonedx-json)."))
    ] = "cyclonedx-json",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help=_("Soubor (jinak stdout). Nepřepisuje."))
    ] = None,
) -> None:
    import json

    from repo_doctor.sbom import FORMATS, sbom_for_repo, write_sbom

    if fmt not in FORMATS:
        _err(
            _("Neznámý formát {fmt!r} (podporováno: {join}).").format(
                fmt=fmt, join=", ".join(FORMATS)
            )
        )
        raise typer.Exit(2)
    path = repo.expanduser()
    if not (path / ".git").exists():
        _err(_("{repo} není git repozitář.").format(repo=repo))
        raise typer.Exit(2)
    try:
        if output is not None:
            write_sbom(path, output.expanduser())
            _err(_("SBOM uložen: {output}").format(output=output))
        else:
            typer.echo(json.dumps(sbom_for_repo(path), indent=2, ensure_ascii=False))
    except FileExistsError:
        _err(_("{output} už existuje – repo-doctor ho nepřepíše.").format(output=output))
        raise typer.Exit(2) from None
    except (ValueError, OSError) as err:
        _err(_("SBOM nejde vytvořit: {err}").format(err=err))
        raise typer.Exit(2) from None


@app.command(
    "mcp",
    help=_(
        "Spustí MCP server (stdio) – pro Claude Code, Claude Desktop apod.\n\nVe výchozím stavu je jen pro čtení: nic neopravuje, nefetchuje ani neklonuje a bez `[mcp] network = true` skenuje offline. Vidí jen repa ve sledovaných složkách. Zápisy (`[mcp] writes`) se zapínají po jednom v Nastavení."
    ),
)
def mcp_server() -> None:
    from repo_doctor.mcpserver import run

    store = ConfigStore()
    try:
        config = store.load()
    except ConfigError as err:
        _err(str(err))
        raise typer.Exit(2) from None
    configure_ssh(batch_mode=config.ssh_batch_mode)
    netpolicy.configure(config.allow_http_hosts)
    run(config, store)


@app.command(
    "update",
    help=_(
        "Zjistí novou verzi; instalaci z tarballu i aktualizuje (po ověření podpisu).\n\nBalíčky .deb/.rpm a AUR aktualizuje správce balíčků systému – vypíše se jen příkaz."
    ),
)
def update_cmd(
    check: Annotated[
        bool, typer.Option("--check", help=_("Jen zjistí, zda je venku nová verze."))
    ] = False,
) -> None:
    from repo_doctor import update

    try:
        release = update.fetch_latest()
    except update.UpdateError as err:
        _err(str(err))
        raise typer.Exit(2) from None
    if release.version <= update.current_version():
        typer.echo(_("✓ Máš nejnovější verzi {version}.").format(version=__version__))
        return
    kind = update.install_kind()
    typer.echo(
        _("Je dostupná verze {version} (máš {version2}).").format(
            version=release.version, version2=__version__
        )
    )
    if check or kind is not update.InstallKind.TARBALL:
        typer.echo(_("Aktualizace: {instructions}").format(instructions=update.instructions(kind)))
        return
    try:
        update.self_update(release, progress=typer.echo)
    except (update.UpdateError, OSError) as err:
        _err(_("Aktualizace selhala: {err}").format(err=err))
        raise typer.Exit(1) from None
    typer.echo(
        _("✓ Aktualizováno na {version} – platí od dalšího spuštění.").format(
            version=release.version
        )
    )


@app.command(
    "tools", help=_("Volitelné nástroje (gitleaks, wl-clipboard, xdg-utils, libnotify) a klíčenka.")
)
def tools_cmd(
    install: Annotated[
        bool, typer.Option("--install", help=_("Doinstaluje chybějící nástroje (po potvrzení)."))
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help=_("Bez dotazu na potvrzení."))] = False,
    update: Annotated[
        bool,
        typer.Option(
            "--update", help=_("Aktualizuje stažené nástroje na verzi připnutou v repo-doctoru.")
        ),
    ] = False,
    remove: Annotated[
        str | None,
        typer.Option(
            "--remove",
            help=_("Odebere nástroj stažený repo-doctorem (např. gitleaks)."),
            metavar=_("NÁSTROJ"),
        ),
    ] = None,
) -> None:
    from repo_doctor import tools

    if remove is not None:
        tool = next((t for t in tools.TOOLS if t.id == remove), None)
        if tool is None:
            _err(
                _("Neznámý nástroj {remove!r} (platné: {join}).").format(
                    remove=remove, join=", ".join(t.id for t in tools.TOOLS)
                )
            )
            raise typer.Exit(2)
        try:
            path = tools.remove_download(tool)
        except tools.ToolsError as err:
            _err(str(err))
            raise typer.Exit(1) from None
        typer.echo(_("✓ {id} odebrán ({path})").format(id=tool.id, path=path))
        return
    statuses = tools.status()
    for st in statuses:
        mark = "✗" if not st.installed else "↑" if st.outdated else "✓"
        where = f"  [{st.source}] {st.path}" if st.path else ""
        typer.echo(f"{mark} {st.tool.id:<13} {st.tool.purpose}{where}")
    ok, detail = tools.keyring_status()
    typer.echo(f"{'✓' if ok else '✗'} {_('klíčenka'):<13} {detail}")
    if update:
        outdated = [st.tool for st in statuses if st.outdated]
        if not outdated:
            typer.echo(_("\nStažené nástroje jsou aktuální."))
            return
        p = tools.update_plan(outdated)
    else:
        missing = [st.tool for st in statuses if not st.installed]
        if not missing:
            if any(st.outdated for st in statuses):
                typer.echo(
                    _("\nNovější verzi stažených nástrojů získáš: repo-doctor tools --update")
                )
            return
        if not install:
            typer.echo(_("\nChybějící nástroje doinstaluješ: repo-doctor tools --install"))
            return
        p = tools.plan(missing, tools.detect_manager())
    p.assume_yes = yes
    typer.echo("")
    for line in tools.describe(p):
        typer.echo(line)
    if not p.command and not p.downloads:
        return
    if not yes:
        if not sys.stdin.isatty():
            _err(_("Bez terminálu potvrď instalaci přepínačem --yes."))
            raise typer.Exit(2)
        if not typer.confirm(_("Pokračovat?"), default=False):
            raise typer.Exit(1)
    try:
        for d in p.downloads:
            path = tools.install_download(d)
            typer.echo(f"✓ {d.name} {d.version} → {path}")
        code = tools.run_command(p)
    except tools.ToolsError as err:
        _err(str(err))
        raise typer.Exit(1) from None
    if code != 0:
        _err(_("Správce balíčků skončil s kódem {code}.").format(code=code))
        raise typer.Exit(1)


@app.command(
    "checks", help=_("Vypíše všechny kontroly (ID, kategorie, severity, automatická oprava).")
)
def list_checks() -> None:
    for c in all_checks():
        fix = _("oprava") if c.fixable else ""
        typer.echo(f"{c.id:<28} {c.category.label:<11} {c.severity.short:<5} {fix}")


@forges_app.command("test", help=_("Ověří připojení nastavených hostingů. Token nikdy nevypíše."))
def forges_test(
    name: Annotated[str | None, typer.Argument(help=_("Název hostingu (jinak všechny)."))] = None,
) -> None:
    from repo_doctor.forges import HTTP_NOTE, build_forge, insecure_reason
    from repo_doctor.forges.tokens import TokenError, resolve_token
    from repo_doctor.sshhelp import forge_ssh_warning

    config = _load_config()
    targets = [f for f in config.forges if name is None or f.name == name]
    if not targets:
        _err(
            _("Hosting {name!r} není nastavený.").format(name=name)
            if name
            else _("Nejsou nastavené žádné hostingy.")
        )
        raise typer.Exit(2)

    async def run_all() -> int:
        failures = 0
        for fc in targets:
            if reason := insecure_reason(fc):
                typer.echo(f"✗ {fc.name}: {reason}")
                failures += 1
                continue
            try:
                token = resolve_token(fc)
            except TokenError as err:
                typer.echo(f"✗ {fc.name}: {err}")
                failures += 1
                continue
            try:
                forge = build_forge(fc, token=token.reveal() if token else None, ttl=0)
            except OSError as err:
                typer.echo(f"✗ {fc.name}: TLS – {err}")
                failures += 1
                continue
            try:
                rep = await forge.test_connection()
                if rep.ok and (ssh := await forge_ssh_warning(forge, fc)):
                    rep.warnings.append(ssh)
            finally:
                await forge.client.aclose()
            if rep.ok:
                typer.echo(
                    _("✓ {name} ({type}): uživatel {v}, vidí {repo_count} repozitářů").format(
                        name=fc.name, type=fc.type, v=rep.user or "?", repo_count=rep.repo_count
                    )
                )
            else:
                typer.echo(f"✗ {fc.name} ({fc.type}): {rep.error}")
                failures += 1
            for w in rep.warnings:
                typer.echo(f"  ! {w}")
            if not fc.verify_tls and not fc.ca_bundle:
                typer.echo(_("  ! ověřování TLS je vypnuté"))
            if fc.base_url.startswith("http://"):
                typer.echo(f"  ! {_(HTTP_NOTE)}")
        return failures

    failures = asyncio.run(run_all())
    raise typer.Exit(1 if failures else 0)


@schedule_app.command(
    "install",
    help=_(
        "Vytvoří ~/.config/systemd/user/repo-doctor.{service,timer} a zapne timer (nic jako root)."
    ),
)
def schedule_install(
    interval: Annotated[str, typer.Option("--interval", help="daily | weekly")] = "weekly",
    time: Annotated[str, typer.Option("--time", help=_("Čas spuštění HH:MM."))] = "08:00",
) -> None:
    from repo_doctor.schedule import ScheduleError, install

    _load_config()
    if interval not in ("daily", "weekly"):
        _err(_("--interval musí být daily nebo weekly."))
        raise typer.Exit(2)
    try:
        files = install(interval, time)  # type: ignore[arg-type]
    except ScheduleError as err:
        _err(str(err))
        raise typer.Exit(2) from None
    typer.echo(_("Plánovaný sken zapnutý:\n") + "\n".join(f"  {f}" for f in files))


@schedule_app.command("status", help=_("Stav plánovaného skenu a poslední běh."))
def schedule_status() -> None:
    from repo_doctor import notify
    from repo_doctor.schedule import describe, status

    st = status()
    if not st.installed:
        typer.echo(_("Plánovaný sken není nainstalovaný (repo-doctor schedule install)."))
    else:
        typer.echo(
            _("Plánovaný sken: {v} · {describe}").format(
                v=_("aktivní") if st.active else _("neaktivní"), describe=describe(st.schedule)
            )
            + (_(" · další běh {next_run}").format(next_run=st.next_run) if st.next_run else "")
        )
    last = notify.load_state()
    if last:
        typer.echo(
            _("Poslední běh: {ts} · {repos} · {v}").format(
                ts=last.ts, repos=czech.repos(last.repos), v=last.changes or _("beze změn")
            )
        )


@schedule_app.command("remove", help=_("Vypne timer a smaže jeho soubory."))
def schedule_remove() -> None:
    from repo_doctor.schedule import remove

    typer.echo(
        _("Plánovaný sken odebrán.") if remove() else _("Plánovaný sken nebyl nainstalovaný.")
    )


@schedule_app.command(
    "test", help=_("Pošle zkušební upozornění všemi nastavenými kanály ([notify]).")
)
def schedule_test() -> None:
    from repo_doctor import notify

    config = _load_config()
    results = notify.deliver(config.notify, _("Zkušební upozornění – plánovaný sken funguje."))
    if not results:
        typer.echo(_("Není nastavený žádný kanál ([notify] desktop, ntfy_url, email)."))
    for d in results:
        typer.echo(f"{'✓' if d.ok else '✗'} {d.channel} {d.detail}".rstrip())
    raise typer.Exit(0 if results and all(d.ok for d in results) else 1)


@config_app.command("path", help=_("Vypíše cestu ke konfiguraci."))
def config_path() -> None:
    typer.echo(str(paths.config_file()))


@config_app.command("validate", help=_("Ověří platnost konfigurace (včetně mapování kláves)."))
def config_validate() -> None:
    store = ConfigStore()
    if not store.exists:
        typer.echo(
            _("Konfigurace neexistuje ({path}) – použijí se výchozí hodnoty.").format(
                path=store.path
            )
        )
        raise typer.Exit(0)
    config = _load_config()
    from repo_doctor.keymap import KeymapError, build_keymap

    try:
        build_keymap(config.keys)
    except KeymapError as err:
        _err(str(err))
        raise typer.Exit(2) from None
    typer.echo(
        _("✓ {path} je platná ({len} složek, {len2} hostingů).").format(
            path=store.path, len=len(config.roots), len2=len(config.forges)
        )
    )


def _print_help() -> None:
    with contextlib.suppress(SystemExit):
        app(["--help"], prog_name="repo-doctor", standalone_mode=True)


def run_tui(roots_override: list[str] | None) -> int:
    from repo_doctor.tui.app import run

    return run(roots_override)


def subcommands() -> set[str]:
    """Názvy podpříkazů CLI (cokoli jiného je složka pro TUI)."""
    names = {
        c.name or (c.callback.__name__.replace("_", "-") if c.callback else "")
        for c in app.registered_commands
    }
    return (names | {g.name for g in app.registered_groups if g.name}) - {""}


def _take_lang(args: list[str]) -> list[str]:
    """Odebere `--lang X` / `--lang=X` (jazyk určil už import balíčku z sys.argv)."""
    rest: list[str] = []
    it = iter(args)
    for arg in it:
        if arg == "--lang":
            i18n.set_language(next(it, ""))
        elif arg.startswith("--lang="):
            i18n.set_language(arg.split("=", 1)[1])
        else:
            rest.append(arg)
    return rest


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    install_log_redaction()
    args = _take_lang(args)
    if not args or (args[0] not in subcommands() and not args[0].startswith("-")):
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            _print_help()
            return 0 if not args else 2
        missing = [a for a in args if not paths.expand_path(a).is_dir()]
        if missing:
            _err(_("Složka neexistuje: {join}").format(join=", ".join(missing)))
            return 2
        return run_tui(args or None)
    try:
        app(args, prog_name="repo-doctor", standalone_mode=True)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
