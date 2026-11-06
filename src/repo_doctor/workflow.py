"""Vyléčit a dotáhnout až do mergi: léčba → push → PR → čekání na CI → merge (#70).

Jen řetězí existující kroky z `fixes` a `publish`, takže platí všechny jejich pojistky: žádný
force, posílá se jen opravná větev, stav se před každým zápisem ověří znovu a **merge rozhoduje
jen `publish.apply_merge`** – po zelené CI. Nic nového se nezapisuje mimo ně.

Workflow se zastaví na prvním problému (CI selhala, CI se nespustila, konflikt, odmítnutí
hostingu, zrušení) a řekne to přes `emit`. Opravná větev i PR zůstanou, takže na ně jde navázat
ručně na stránce Odeslání, nebo znovu `publish_existing` – ta přeskočí kroky, které už jsou hotové.
Funkce jsou synchronní (uvnitř `asyncio.run`) a patří do pracovního vlákna.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import httpx

from repo_doctor import publish
from repo_doctor.config import Config
from repo_doctor.fixes import FixRefused, apply
from repo_doctor.forges.base import CIStatus, PullRequestInfo
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.httpclient import HttpError
from repo_doctor.i18n import _
from repo_doctor.masking import redact
from repo_doctor.models import Patch

POLL_SECONDS = 15.0  # jak často se ptát hostingu na CI
CI_GRACE_SECONDS = 90.0  # tak dlouho se čeká, až se po vytvoření PR CI vůbec objeví
CI_TIMEOUT_SECONDS = 45 * 60.0  # nejdéle čekání na doběhnutí CI
MERGEABLE_RETRIES = 8  # hosting ještě zjišťuje, jestli jde PR mergnout

Level = Literal["info", "ok", "warn", "error"]
Kind = Literal[
    "healed",  # větev s opravami vznikla
    "pushed",
    "pr",
    "waiting",  # průběh (CI běží) – neukládá se mezi notifikace
    "merged",
    "nothing",  # hotovo bez odesílání (jen lokální opravy)
    "ci_failed",
    "no_ci",
    "timeout",
    "blocked",
    "cancelled",
    "refused",
]
# Události, které workflow ukončují (úspěšně nebo zastavením).
FINAL: frozenset[Kind] = frozenset(
    {"merged", "nothing", "ci_failed", "no_ci", "timeout", "blocked", "cancelled", "refused"}
)


@dataclass
class Event:
    kind: Kind
    level: Level
    text: str
    url: str = ""

    @property
    def stored(self) -> bool:
        """Do notifikací patří vše kromě průběžného „čekám na CI“."""
        return self.kind != "waiting"

    @property
    def final(self) -> bool:
        return self.kind in FINAL


class Stop(Exception):
    """Workflow se zastaví – bere se jako výsledek, ne jako chyba programu."""

    def __init__(self, event: Event) -> None:
        super().__init__(event.text)
        self.event = event


Sleep = Callable[[float], Awaitable[None]]


@dataclass
class Context:
    """Vše, co workflow potřebuje zvenku (a co se v testech nahrazuje)."""

    emit: Callable[[Event], None]
    transport: httpx.AsyncBaseTransport | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    sleep: Sleep | None = None
    poll: float = POLL_SECONDS
    grace: float = CI_GRACE_SECONDS
    timeout: float = CI_TIMEOUT_SECONDS

    async def wait(self, seconds: float) -> None:
        """Čeká po částech, aby šlo čekání přerušit (zavření aplikace, zrušení)."""
        if self.sleep is not None:
            await self.sleep(seconds)
        else:
            left = seconds
            while left > 0 and not self.cancel.is_set():
                step = min(0.5, left)
                await asyncio.sleep(step)
                left -= step
        self.check_cancel()

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Stop(
                Event(
                    "cancelled",
                    "warn",
                    _(
                        "Workflow přerušeno. Opravná větev a případný PR zůstaly – dokončit je jde na stránce Odeslání."
                    ),
                )
            )


# ---------------------------------------------------------------------------- vstupy


def heal_and_publish(
    git: Git, config: Config, patches: list[Patch], ctx: Context, *, today: date | None = None
) -> None:
    """Aplikuje vybrané opravy a dotáhne je přes push a PR až do mergu. Výsledek jde do `emit`."""
    _run(_heal_and_publish(git, config, patches, ctx, today), ctx)


def publish_existing(git: Git, config: Config, ctx: Context, branch: str | None = None) -> None:
    """Dotáhne už existující opravnou větev (push, PR, CI, merge); hotové kroky přeskočí."""
    _run(_publish_existing(git, config, ctx, branch), ctx)


def _run(coro: Awaitable[None], ctx: Context) -> None:
    try:
        asyncio.run(_guard(coro))
    except Stop as stop:
        ctx.emit(stop.event)


async def _guard(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Stop:
        raise
    except publish.PublishRefused as err:
        raise Stop(Event("refused", "error", str(err))) from err
    except FixRefused as err:
        raise Stop(Event("refused", "error", str(err))) from err
    except GitError as err:
        raise Stop(
            Event("refused", "error", _("git selhal: {err}").format(err=err.stderr))
        ) from err
    except HttpError as err:
        raise Stop(
            Event("refused", "error", publish.describe_http_error(err, _("práce s hostingem")))
        ) from err
    except OSError as err:
        raise Stop(Event("refused", "error", str(err))) from err


async def _heal_and_publish(
    git: Git, config: Config, patches: list[Patch], ctx: Context, today: date | None
) -> None:
    ctx.check_cancel()
    hosting: publish.Hosting | None = None
    try:
        if any(p.changes for p in patches):
            # hosting se ověří dřív než léčba, aby se kvůli chybějícímu tokenu netvořila větev zbytečně
            hosting = await publish.connect(
                git, config, publish.pick_remote(git, config), transport=ctx.transport
            )
        result = apply(git, patches, today=today)
        n = len(result.commits)
        if result.branch is None:
            ctx.emit(
                Event(
                    "nothing",
                    "ok",
                    _("Léčba hotová, lokální změny v .git/ provedeny. Není co odesílat."),
                )
            )
            return
        ctx.emit(
            Event(
                "healed",
                "info",
                _("Větev {branch} vznikla ({n} {commits}).").format(
                    branch=result.branch, n=n, commits=_commits(n)
                ),
            )
        )
        if hosting is None:  # pragma: no cover - větev vzniká jen z oprav souborů
            raise Stop(Event("refused", "error", _("Chybí spojení s hostingem.")))
        await _chain(git, hosting, config, result.branch, ctx)
    finally:
        if hosting is not None:
            await hosting.aclose()


async def _publish_existing(git: Git, config: Config, ctx: Context, branch: str | None) -> None:
    ctx.check_cancel()
    name = publish.resolve_branch(git, branch)
    hosting = await publish.connect(
        git, config, publish.pick_remote(git, config), transport=ctx.transport
    )
    try:
        await _chain(git, hosting, config, name, ctx)
    finally:
        await hosting.aclose()


def _commits(n: int) -> str:
    return _("commit") if n == 1 else _("commity") if 1 < n < 5 else _("commitů")


# ---------------------------------------------------------------------------- řetěz


async def _chain(
    git: Git, hosting: publish.Hosting, config: Config, branch: str, ctx: Context
) -> None:
    _push(git, hosting, config, branch, ctx)
    pr = await _ensure_pr(git, hosting, config, branch, ctx)
    if pr.state == "merged":
        ctx.emit(
            Event(
                "merged",
                "ok",
                _("PR #{number} už je mergnutý.").format(number=pr.number),
                pr.url,
            )
        )
        return
    await _wait_and_merge(git, hosting, config, branch, ctx)


def _push(git: Git, hosting: publish.Hosting, config: Config, branch: str, ctx: Context) -> None:
    ctx.check_cancel()
    sha = git.rev_parse(f"refs/heads/{branch}")
    if git.rev_parse(f"refs/remotes/{hosting.remote}/{branch}") == sha:
        return  # už je poslaná
    plan = publish.plan_push(git, config, branch)
    publish.apply_push(git, plan, config)
    ctx.emit(
        Event(
            "pushed",
            "info",
            _("Větev {branch} poslána na {remote}.").format(branch=branch, remote=plan.remote),
        )
    )


async def _ensure_pr(
    git: Git, hosting: publish.Hosting, config: Config, branch: str, ctx: Context
) -> PullRequestInfo:
    ctx.check_cancel()
    existing = await hosting.forge.find_pr(hosting.repo, branch)
    if existing is not None and existing.state in ("open", "merged"):
        return existing
    plan = await publish.plan_pr(git, hosting, config, branch)
    pr = await publish.apply_pr(git, hosting, config, plan)
    ctx.emit(
        Event(
            "pr",
            "info",
            _("PR #{number} vytvořen, čekám na CI.").format(number=pr.number),
            pr.url,
        )
    )
    return pr


def _progress(ci: CIStatus) -> str:
    if not ci.checks:
        return _("CI běží…")
    done = sum(1 for c in ci.checks if c.state not in ("pending", "none", "unknown"))
    return _("CI běží ({done}/{total} kontrol)").format(done=done, total=len(ci.checks))


async def _wait_and_merge(
    git: Git, hosting: publish.Hosting, config: Config, branch: str, ctx: Context
) -> None:
    elapsed = 0.0
    mergeable_waits = 0
    last = ""
    while True:
        ctx.check_cancel()
        plan = await publish.plan_merge(git, hosting, config, branch)
        pr, ci = plan.pr, plan.ci
        state = ci.state
        if state == "failure":
            failed = [c.name for c in ci.checks if c.state == "failure"]
            detail = f" ({', '.join(failed[:5])})" if failed else ""
            raise Stop(
                Event(
                    "ci_failed",
                    "error",
                    _(
                        "CI u PR #{number} selhala{detail}. Merge se nekoná, PR zůstává otevřený."
                    ).format(number=pr.number, detail=redact(detail)),
                    pr.url,
                )
            )
        if state == "unknown":
            raise Stop(
                Event(
                    "blocked",
                    "warn",
                    _(
                        "Stav CI u PR #{number} nejde zjistit (token nemá právo ho číst) – mergni ručně, pokud je to v pořádku."
                    ).format(number=pr.number),
                    pr.url,
                )
            )
        if state == "none" and elapsed >= ctx.grace:
            raise Stop(
                Event(
                    "no_ci",
                    "warn",
                    _(
                        "U PR #{number} se CI nespustila (ani po {sec} s) – workflow nemerguje naslepo. Mergni ručně, pokud CI nemáš."
                    ).format(number=pr.number, sec=int(ctx.grace)),
                    pr.url,
                )
            )
        if state == "success":
            if plan.ready:
                await _merge(git, hosting, config, plan, ctx)
                return
            if pr.mergeable is None and mergeable_waits < MERGEABLE_RETRIES:
                mergeable_waits += 1
            else:
                raise Stop(
                    Event(
                        "blocked",
                        "warn",
                        _("CI u PR #{number} je zelená, ale merge nejde: {why}").format(
                            number=pr.number, why=" ".join(plan.blockers)
                        ),
                        pr.url,
                    )
                )
        text = _progress(ci) if state == "pending" else _("Čekám, až se spustí CI…")
        if state == "success":
            text = _("CI je zelená, čekám na hosting…")
        if text != last:
            last = text
            ctx.emit(Event("waiting", "info", text, pr.url))
        if elapsed >= ctx.timeout:
            raise Stop(
                Event(
                    "timeout",
                    "warn",
                    _(
                        "CI u PR #{number} neskončila do {min} minut – workflow končí, PR zůstává otevřený."
                    ).format(number=pr.number, min=int(ctx.timeout // 60)),
                    pr.url,
                )
            )
        await ctx.wait(ctx.poll)
        elapsed += ctx.poll


async def _merge(
    git: Git, hosting: publish.Hosting, config: Config, plan: publish.MergePlan, ctx: Context
) -> None:
    ctx.check_cancel()
    done = await publish.apply_merge(git, hosting, config, plan)
    if done.state == "merged":
        ctx.emit(
            Event(
                "merged",
                "ok",
                _("PR #{number} mergnut po zelené CI. Změny stáhneš v Synchronizaci.").format(
                    number=done.number
                ),
                done.url,
            )
        )
    else:
        ctx.emit(
            Event(
                "blocked",
                "warn",
                _("Hosting merge přijal, PR #{number} ale zatím není mergnutý.").format(
                    number=done.number
                ),
                done.url,
            )
        )
