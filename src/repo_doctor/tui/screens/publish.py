"""Stránka Odeslání oprav: push, PR/MR, merge na hostingu, lokální merge a stav CI.

Stránka (`PublishScreen`) ukazuje, kde je opravná větev na cestě ven, včetně jednotlivých kontrol
CI pull requestu – bez doběhlé zelené CI se nemá mergovat. Každý krok je samostatná akce: v pozadí se připraví náhled (`publish.plan_*`, nic nemění),
ukáže se v potvrzovacím dialogu s výchozí volbou „Zrušit“ a teprve po potvrzení se provede
(`publish.apply_*`, který stav před zápisem ověří znovu).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Static

from repo_doctor import czech, publish
from repo_doctor.config import Config
from repo_doctor.forges.base import CIState, PullRequestInfo
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.i18n import _
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.screens.base import BaseScreen
from repo_doctor.tui.screens.dialogs import ConfirmDialog
from repo_doctor.tui.util import open_url
from repo_doctor.tui.widgets.keybar import KeyBar

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp

Kind = Literal["push", "push_default", "pr", "merge", "merge_local"]
MAX_PREVIEW_LINES = 14
REFRESH_SECONDS = 20  # dokud CI běží, stránka se sama obnovuje


@dataclass
class Prepared:
    """Připravený krok: náhled pro dialog a funkce, která ho provede (běží ve vlákně)."""

    title: str
    body: Text
    confirm_label: str
    done_title: str
    run: Callable[[], str]  # vrací zprávu o výsledku


class Blocked(Exception):
    """Krok je připravený, ale něco mu brání (např. běžící CI) – ukáže se jako upozornění."""


class PublishFlow:
    def __init__(
        self, screen: Screen[None], path: str, on_done: Callable[[], None] | None = None
    ) -> None:
        self.screen = screen
        self.path = path
        self.on_done = on_done  # po dokončení kroku (úspěšném i ne) – např. obnovení stavu

    @property
    def rd(self) -> RepoDoctorApp:
        return self.screen.app  # type: ignore[return-value]

    # ------------------------------------------------------------------ spuštění
    def start(self, kind: Kind) -> None:
        self.screen.run_worker(
            partial(self._prepare, kind), thread=True, exclusive=True, group="publish"
        )

    def _call(self, fn: Callable[..., object], *args: object, **kwargs: object) -> None:
        self.screen.app.call_from_thread(partial(fn, *args, **kwargs))

    def _prepare(self, kind: Kind) -> None:
        config, git = self.rd.config, Git(Path(self.path))
        try:
            prepared = {
                "push": partial(self._push, git, config, default=False),
                "push_default": partial(self._push, git, config, default=True),
                "pr": partial(self._pr, git, config),
                "merge": partial(self._merge, git, config),
                "merge_local": partial(self._merge_local, git, config),
            }[kind]()
        except Blocked as err:
            self._call(
                self.screen.notify,
                str(err),
                title=_("Merge zatím nejde"),
                severity="warning",
                timeout=15,
            )
            return
        except publish.PublishRefused as err:
            self._call(
                self.screen.notify,
                str(err),
                title=_("Odeslání nejde"),
                severity="warning",
                timeout=15,
            )
            return
        except (GitError, OSError) as err:
            self._call(self.screen.notify, str(err), title=_("Odeslání selhalo"), severity="error")
            return
        self._call(self._confirm, prepared)

    def _confirm(self, prepared: Prepared) -> None:
        def decided(ok: bool | None) -> None:
            if ok:
                self.screen.run_worker(
                    partial(self._run, prepared), thread=True, exclusive=True, group="publish-run"
                )
            else:
                self.screen.notify(_("Zrušeno – nic se nezměnilo."))

        self.screen.app.push_screen(
            ConfirmDialog(prepared.title, prepared.body, confirm_label=prepared.confirm_label),
            decided,
        )

    def _run(self, prepared: Prepared) -> None:
        try:
            message = prepared.run()
        except publish.PublishRefused as err:
            self._call(
                self.screen.notify, str(err), title=_("Odeslání se nepovedlo"), severity="error",
                timeout=20,
            )  # fmt: skip
            return
        except (GitError, OSError) as err:
            self._call(self.screen.notify, str(err), title=_("Odeslání selhalo"), severity="error")
            return
        finally:
            if self.on_done is not None:
                self._call(self.on_done)
        self._call(self.screen.notify, message, title=prepared.done_title, timeout=20)

    # ------------------------------------------------------------------ kroky
    def _text(self, *parts: str) -> Text:
        return Text("\n".join(parts), style=Style(color=self.rd.palette.text))

    def _commit_lines(self, commits: list[publish.CommitInfo]) -> str:
        lines = [f"  {c.short} {c.subject}" for c in commits[:MAX_PREVIEW_LINES]]
        if len(commits) > MAX_PREVIEW_LINES:
            lines.append("  " + czech.more(len(commits) - MAX_PREVIEW_LINES))
        return "\n".join(lines)

    def _hosting[T](
        self, git: Git, config: Config, work: Callable[[publish.Hosting], Awaitable[T]]
    ) -> T:
        transport = self.rd.forge_transport
        return asyncio.run(publish.with_hosting(git, config, work, transport=transport))

    def _push(self, git: Git, config: Config, *, default: bool) -> Prepared:
        plan = publish.plan_push(git, config, default=default)
        body = self._text(
            "",
            _("Pošle větev {branch} na {remote} ({url}), {commits}:").format(
                branch=plan.branch,
                remote=plan.remote,
                url=plan.remote_url,
                commits=czech.commits(len(plan.commits)),
            ),
            self._commit_lines(plan.commits),
            "",
            _("Jen tahle jedna větev, nikdy force ani tagy. Nové commity jsou zkontrolované na tajemství."),
        )  # fmt: skip

        def run() -> str:
            publish.apply_push(git, plan, config)
            return _("Odesláno: {branch} → {remote}.").format(
                branch=plan.branch, remote=plan.remote
            )

        return Prepared(_("Poslat větev (push)"), body, _("Poslat"), _("Push hotový"), run)

    def _pr(self, git: Git, config: Config) -> Prepared:
        async def plan_it(hosting: publish.Hosting) -> publish.PRPlan:
            return await publish.plan_pr(git, hosting, config)

        plan = self._hosting(git, config, plan_it)
        preview = plan.body.splitlines()
        if len(preview) > MAX_PREVIEW_LINES:
            preview = [*preview[:MAX_PREVIEW_LINES], "…"]
        body = self._text(
            "",
            _("PR na {forge} ({owner_path}): {head} → {base}").format(
                forge=plan.forge, owner_path=plan.owner_path, head=plan.branch, base=plan.base
            ),
            "",
            plan.title,
            "",
            *preview,
        )

        def run() -> str:
            async def apply_it(hosting: publish.Hosting) -> PullRequestInfo:
                return await publish.apply_pr(git, hosting, config, plan)

            pr = self._hosting(git, config, apply_it)
            return _("Vytvořen PR #{number}: {url}").format(number=pr.number, url=pr.url)

        return Prepared(_("Vytvořit PR/MR"), body, _("Vytvořit"), _("PR vytvořen"), run)

    def _merge(self, git: Git, config: Config) -> Prepared:
        async def plan_it(hosting: publish.Hosting) -> publish.MergePlan:
            return await publish.plan_merge(git, hosting, config)

        plan = self._hosting(git, config, plan_it)
        head = _("PR #{number} {title}\n{url}\nCI: {ci} · metoda: {method}").format(
            number=plan.pr.number,
            title=plan.pr.title,
            url=plan.pr.url,
            ci=plan.ci.state,
            method=plan.method or _("výchozí hostingu"),
        )
        if not plan.ready:
            raise Blocked(head + "\n" + "\n".join(f"✗ {b}" for b in plan.blockers))
        body = self._text(
            "",
            head,
            "",
            _(
                "CI je zelená a hosting hlásí, že jde mergnout. Merge na hostingu nejde vrátit zpět."
            ),
        )

        def run() -> str:
            async def apply_it(hosting: publish.Hosting) -> PullRequestInfo:
                return await publish.apply_merge(git, hosting, config, plan)

            done = self._hosting(git, config, apply_it)
            if done.state != "merged":
                return _("Hosting merge přijal, PR zatím není mergnutý: {url}").format(url=done.url)
            return _("PR #{number} mergnut. Změny stáhneš v Synchronizaci ({key}).").format(
                number=done.number, key=self.rd.key("screen_sync")
            )

        return Prepared(_("Mergnout PR"), body, _("Mergnout"), _("Merge hotový"), run)

    def _merge_local(self, git: Git, config: Config) -> Prepared:
        plan = publish.plan_merge_local(git, config)
        tree = (
            _("Změní se {files} v pracovním stromě.").format(files=czech.files(len(plan.files)))
            if plan.checked_out
            else _("Pracovní strom se nezmění (větev {target} není checkoutnutá).").format(
                target=plan.target
            )
        )
        body = self._text(
            "",
            _("Posune {target} na {branch} (jen fast-forward, bez merge commitu), {commits}:").format(
                target=plan.target, branch=plan.branch, commits=czech.commits(len(plan.commits))
            ),
            self._commit_lines(plan.commits),
            "",
            tree + " " + _("Nic se nepushne."),
        )  # fmt: skip

        def run() -> str:
            publish.apply_merge_local(git, config, plan)
            return _("{target} posunuta na {branch}. Chceš ji poslat? Klávesa {key}.").format(
                target=plan.target, branch=plan.branch, key=self.rd.key("publish_push_default")
            )

        return Prepared(_("Lokální merge"), body, _("Mergnout"), _("Merge hotový"), run)


# ---------------------------------------------------------------------------- stránka

CI_MARKS: dict[CIState, str] = {
    "success": "✓",
    "failure": "✗",
    "pending": "…",
    "none": "–",
    "unknown": "?",
}


class PublishScreen(BaseScreen):
    """Přehled cesty opravné větve ven (push → PR → CI → merge) a klávesy pro jednotlivé kroky."""

    help_context = "publish"
    BINDINGS = [*BaseScreen.BINDINGS, *bindings("publish")]

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.status: publish.PublishStatus | None = None
        self.flow = PublishFlow(self, path, on_done=self.refresh_status)
        self._timer: Timer | None = None

    @property
    def rd(self) -> RepoDoctorApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(classes="page panel") as page:
            page.border_title = _(" odeslání oprav ")
            yield Static(id="pub-head", classes="page-header")
            yield Static(id="pub-steps")
            yield Static(id="pub-ci-title")
            table: DataTable[str | Text] = DataTable(id="pub-ci", cursor_type="row")
            yield table
            yield Static(id="pub-note")
        yield KeyBar(
            [
                ("publish_push", _("push")),
                ("publish_pr", _("PR/MR")),
                ("publish_merge", _("merge")),
                ("publish_merge_local", _("lokální merge")),
                ("publish_push_default", _("push výchozí")),
                ("publish_refresh", _("obnovit")),
                ("publish_open_web", _("web")),
                ("esc", _("zpět")),
                "help",
            ]
        )

    def on_mount(self) -> None:
        repo = self.rd.repo(self.path)
        name = repo.name if repo is not None else Path(self.path).name
        self.query_one("#pub-head", Static).update(
            Text(name, style=Style(color=self.rd.palette.accent, bold=True))
        )
        self.query_one("#pub-steps", Static).update(
            Text(_("Zjišťuji stav…"), style=Style(color=self.rd.palette.muted))
        )
        self.refresh_status()
        self.query_one(DataTable).focus()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    # ------------------------------------------------------------------ načtení stavu
    def refresh_status(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.load()

    @work(thread=True, exclusive=True, group="publish-status")
    def load(self) -> None:
        git, rd = Git(Path(self.path)), self.rd
        try:
            status = asyncio.run(
                publish.publish_status(
                    git, rd.config, online=not rd.offline, transport=rd.forge_transport
                )
            )
        except (GitError, OSError) as err:
            status = publish.PublishStatus(local_note=str(err))
        self.app.call_from_thread(self._loaded, status)

    def _loaded(self, status: publish.PublishStatus) -> None:
        self.status = status
        self.render_status()
        if status.ci is not None and status.ci.state == "pending":
            self._timer = self.set_timer(REFRESH_SECONDS, self.refresh_status)

    # ------------------------------------------------------------------ vykreslení
    def _line(self, mark: str, color: str, label: str, text: str, hint: str = "") -> Text:
        p = self.rd.palette
        line = Text(" ")
        line.append(f"{mark} ", style=Style(color=color, bold=True))
        line.append(f"{label:<10}", style=Style(color=p.muted))
        line.append(text, style=Style(color=p.text))
        if hint:
            line.append(f"   {hint}", style=Style(color=p.accent))
        return line

    def _ci_color(self, state: CIState) -> str:
        p = self.rd.palette
        return {
            "success": p.ok,
            "failure": p.high,
            "pending": p.med,
            "none": p.dim,
            "unknown": p.med,
        }[state]

    def render_status(self) -> None:
        st = self.status
        if st is None:
            return
        p, key = self.rd.palette, self.rd.key
        head = self.query_one("#pub-head", Static)
        repo = self.rd.repo(self.path)
        name = repo.name if repo is not None else Path(self.path).name
        title = Text(name, style=Style(color=p.accent, bold=True))
        if st.branch:
            title.append(f"   {st.branch}", style=Style(color=p.text))
            title.append(
                f"  ({czech.commits(len(st.commits))}"
                + (f" → {st.remote}" if st.remote else "")
                + ")",
                style=Style(color=p.muted),
            )
        head.update(title)

        lines: list[Text] = []
        if st.branch is None:
            lines.append(
                self._line("–", p.dim, _("větev"), st.local_note or _("bez opravné větve"))
            )
            if st.default_ahead:
                lines += self._default_lines(st)
            self.query_one("#pub-steps", Static).update(Text("\n").join(lines))
            self._fill_ci(None)
            self._note(st)
            return
        lines.append(self._push_line(st))
        lines.append(self._pr_line(st))
        lines.append(self._ci_line(st))
        lines += self._merge_lines(st)
        lines.append(
            self._line(
                "✓" if st.can_merge_local else "–",
                p.ok if st.can_merge_local else p.dim,
                _("lokálně"),
                _("výchozí větev {default} jde posunout fast-forwardem").format(default=st.default)
                if st.can_merge_local
                else _("lokální merge teď nejde (nebo už není co mergovat)"),
                key("publish_merge_local") if st.can_merge_local else "",
            )
        )
        lines += self._default_lines(st)
        self.query_one("#pub-steps", Static).update(Text("\n").join(lines))
        self._fill_ci(st)
        self._note(st)

    def _push_line(self, st: publish.PublishStatus) -> Text:
        p = self.rd.palette
        label = _("1 push")
        if st.pushed is None:
            return self._line("–", p.dim, label, _("repo nemá remote – push nejde"))
        if st.pushed:
            return self._line("✓", p.ok, label, _("poslána na {remote}").format(remote=st.remote))
        return self._line(
            "✗",
            p.med,
            label,
            _("na {remote} zatím není").format(remote=st.remote),
            self.rd.key("publish_push"),
        )

    def _pr_line(self, st: publish.PublishStatus) -> Text:
        p, label = self.rd.palette, _("2 PR/MR")
        pr = st.pr
        if pr is None:
            if st.hosting_note:
                return self._line("?", p.med, label, _("stav hostingu není znám"))
            if not st.pushed:
                return self._line("–", p.dim, label, _("nejdřív pošli větev (push)"))
            return self._line(
                "✗", p.med, label, _("žádný PR/MR zatím není"), self.rd.key("publish_pr")
            )
        state = {"open": _("otevřený"), "merged": _("mergnutý"), "closed": _("zavřený")}[pr.state]
        color = {"open": p.ok, "merged": p.ok, "closed": p.high}[pr.state]
        mark = "✗" if pr.state == "closed" else "✓"
        return self._line(mark, color, label, f"#{pr.number} {state}  {pr.url}")

    def _ci_line(self, st: publish.PublishStatus) -> Text:
        p, label = self.rd.palette, _("3 CI")
        ci = st.ci
        if ci is None:
            return self._line("–", p.dim, label, _("bez PR/MR není co sledovat"))
        total = len(ci.checks)
        bad = sum(1 for c in ci.checks if c.state == "failure")
        running = sum(1 for c in ci.checks if c.state == "pending")
        texts: dict[CIState, str] = {
            "success": _("prošla ({n})").format(n=czech.checks(total)) if total else _("prošla"),
            "failure": _("selhala – {bad} z {n} kontrol").format(bad=bad, n=total)
            if total
            else _("selhala"),
            "pending": _("běží – {running} z {n} kontrol ještě neskončilo").format(
                running=running, n=total
            )
            if total
            else _("běží"),
            "none": _("žádná CI se nespustila"),
            "unknown": _("stav nejde zjistit (token nemá právo ho číst)"),
        }
        return self._line(CI_MARKS[ci.state], self._ci_color(ci.state), label, texts[ci.state])

    def _merge_lines(self, st: publish.PublishStatus) -> list[Text]:
        p, label = self.rd.palette, _("4 merge")
        pr = st.pr
        if pr is None:
            return [self._line("–", p.dim, label, _("nejdřív PR/MR a zelená CI"))]
        if pr.state == "merged":
            return [
                self._line(
                    "✓",
                    p.ok,
                    label,
                    _("hotovo – změny stáhneš v Synchronizaci ({key})").format(
                        key=self.rd.key("screen_sync")
                    ),
                )
            ]
        if pr.state == "closed":
            return [self._line("✗", p.high, label, _("PR/MR je zavřený"))]
        if st.can_merge:
            return [
                self._line(
                    "✓", p.ok, label, _("jde mergnout, CI je zelená"), self.rd.key("publish_merge")
                )
            ]
        lines = [self._line("✗", p.high, label, _("zatím nejde mergnout:"))]
        for blocker in st.blockers:
            lines.append(Text(f"     • {blocker}", style=Style(color=p.muted)))
        return lines

    def _default_lines(self, st: publish.PublishStatus) -> list[Text]:
        if not st.default_ahead or st.remote is None:
            return []
        return [
            self._line(
                "✗",
                self.rd.palette.med,
                _("výchozí"),
                _("{default} má {commits}, které nejsou na {remote}").format(
                    default=st.default, commits=czech.commits(st.default_ahead), remote=st.remote
                ),
                self.rd.key("publish_push_default"),
            )
        ]

    def _fill_ci(self, st: publish.PublishStatus | None) -> None:
        p = self.rd.palette
        title = self.query_one("#pub-ci-title", Static)
        table = self.query_one(DataTable)
        table.clear(columns=True)
        checks = st.ci.checks if st is not None and st.ci is not None else []
        if st is None or st.ci is None:
            title.update("")
            return
        title.update(
            Text(
                "\n " + _("Kontroly CI ({n})").format(n=len(checks)),
                style=Style(color=p.accent, bold=True),
            )
        )
        table.add_columns(" ", _("kontrola"), _("odkaz"))
        for check in checks:
            table.add_row(
                Text(CI_MARKS[check.state], style=Style(color=self._ci_color(check.state))),
                check.name,
                Text(check.url or "—", style=Style(color=p.muted)),
                key=check.url or check.name,
            )
        if not checks:
            table.add_row(
                "",
                Text(_("hosting nehlásí jednotlivé kontroly"), style=Style(color=p.muted)),
                "",
            )

    def _note(self, st: publish.PublishStatus) -> None:
        text = ""
        if st.hosting_note:
            text = st.hosting_note
        self.query_one("#pub-note", Static).update(
            Text("\n " + text if text else "", style=Style(color=self.rd.palette.muted))
        )

    # ------------------------------------------------------------------ akce
    @on(DataTable.RowSelected)
    def _open_check(self, event: DataTable.RowSelected) -> None:
        url = str(event.row_key.value or "")
        if url.startswith(("https://", "http://")) and not open_url(url):
            self.notify(_("xdg-open není k dispozici: {web_url}").format(web_url=url))

    def action_publish_push(self) -> None:
        self.flow.start("push")

    def action_publish_push_default(self) -> None:
        self.flow.start("push_default")

    def action_publish_pr(self) -> None:
        self.flow.start("pr")

    def action_publish_merge(self) -> None:
        self.flow.start("merge")

    def action_publish_merge_local(self) -> None:
        self.flow.start("merge_local")

    def action_publish_refresh(self) -> None:
        self.refresh_status()

    def action_publish_open_web(self) -> None:
        pr = self.status.pr if self.status else None
        if pr is None:
            self.notify(_("Zatím není žádný PR/MR."), severity="warning")
        elif not open_url(pr.url):
            self.notify(_("xdg-open není k dispozici: {web_url}").format(web_url=pr.url))
