"""Léčba: výběr oprav (předpis), barevný náhled diffu a potvrzení."""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from repo_doctor.checks.base import RepoContext
from repo_doctor.config import ConfigError, effective_for_repo, load_repo_overrides
from repo_doctor.czech import count
from repo_doctor.fixes import ApplyResult, FixRefused, apply, branch_name, plan
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.i18n import _
from repo_doctor.models import Patch, RepoResult
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.screens.base import BaseScreen
from repo_doctor.tui.screens.dialogs import ConfirmDialog
from repo_doctor.tui.theme import Palette
from repo_doctor.tui.widgets.keybar import KeyBar
from repo_doctor.tui.widgets.panel import split_title

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp


def diff_text(diff: str, p: Palette, width: int) -> Text:
    t = Text()
    for raw in diff.splitlines():
        line = raw.ljust(max(width, len(raw)))
        if raw.startswith(("+++", "---")):
            style = Style(color=p.muted)
        elif raw.startswith("+"):
            style = Style(color=p.ok, bgcolor=p.diff_add)
        elif raw.startswith("-"):
            style = Style(color=p.high, bgcolor=p.diff_del)
        elif raw.startswith("@@"):
            style = Style(color=p.accent)
        else:
            style = Style(color=p.text)
        t.append(line + "\n", style=style)
    return t


def commits_word(n: int) -> str:
    return _("commit") if n == 1 else _("commity") if 1 < n < 5 else _("commitů")


class FixScreen(BaseScreen):
    help_context = "fix"
    BINDINGS = [
        *BaseScreen.BINDINGS,
        *[b for b in bindings("fix") if b.id != "confirm_fix"],
        Binding(
            "enter", "confirm_fix", _("potvrdit"), key_display="⏎", priority=True, id="confirm_fix"
        ),
    ]

    def __init__(self, path: str, focus_check: str | None = None) -> None:
        super().__init__()
        self.path = path
        self.focus_check = focus_check
        self.patches: list[Patch] = []
        self.branch = ""
        self.dirty = 0
        self.ready = False
        self.current: Patch | None = None
        self.selected: set[str] | None = None

    @property
    def rd(self) -> RepoDoctorApp:
        return self.app  # type: ignore[return-value]

    @property
    def repo(self) -> RepoResult | None:
        return self.rd.repo(self.path)

    def compose(self) -> ComposeResult:
        yield Static(id="fix-header")
        with Horizontal(id="fix-body"):
            with Vertical(id="rx-panel"):
                yield OptionList(id="rx")
                yield Static(id="rx-summary")
            with VerticalScroll(id="diff-panel"):
                yield Static(id="diff")
        yield KeyBar(
            [
                "toggle_fix",
                "select_all",
                ("j/k", _("pohyb")),
                "confirm_fix",
                "fix_publish",
                ("esc", _("zpět")),
                "help",
            ]
        )

    def on_mount(self) -> None:
        self.query_one("#rx-panel").border_title = _(" PŘEDPIS ")
        self.query_one("#diff-panel").border_title = _(" NÁHLED ZMĚN ")
        self._header(_("připravuji opravy…"))
        self.prepare()

    def on_resize(self, event: Resize) -> None:
        self._render_diff()

    def on_theme_changed(self) -> None:
        self._render_list()
        self._render_diff()

    def _header(self, message: str | Text) -> None:
        repo = self.repo
        header = self.query_one("#fix-header", Static)
        n = len(self.patches)
        right = count(n, _("opravitelný nález"), _("opravitelné nálezy"), _("opravitelných nálezů"))
        split_title(
            header,
            _("léčba · {v}").format(v=repo.name if repo else "?"),
            right if self.ready else "",
        )
        header.update(message)

    def _repo_context(self, repo: RepoResult) -> RepoContext:
        path = Path(repo.path)
        config = self.rd.config
        with contextlib.suppress(ConfigError):
            config = effective_for_repo(config, load_repo_overrides(path))
        return RepoContext(
            path=path,
            name=repo.name,
            root=repo.root,
            config=config,
            git=Git(path),
            now=self.rd.now(),
        )

    @work(thread=True, exclusive=True, group="fix-prepare")
    def prepare(self) -> None:
        repo = self.repo
        if repo is None:
            self.app.call_from_thread(self._header, _("Repo už není ve výsledcích skenu."))
            return
        try:
            ctx = self._repo_context(repo)
            patches = plan(ctx, [f for f in repo.findings if f.fixable])
            dirty = ctx.git.status().dirty
            branch = branch_name(ctx.git, self.rd.now().date())
        except (GitError, OSError) as err:
            self.app.call_from_thread(
                self._header,
                Text(
                    _("Opravy nejde připravit: {err}").format(err=err),
                    style=Style(color=self.rd.palette.high),
                ),
            )
            return
        self.app.call_from_thread(self._prepared, patches, dirty, branch)

    def _prepared(self, patches: list[Patch], dirty: int, branch: str) -> None:
        self.patches, self.dirty, self.branch, self.ready = patches, dirty, branch, True
        p = self.rd.palette
        if not patches:
            self._header(
                Text(
                    _("Žádné automaticky opravitelné nálezy. Esc zpět."), style=Style(color=p.muted)
                )
            )
        elif dirty:
            local = (
                _(" Jen lokální úpravy (.git/) jde provést i tak.")
                if any(pt.local_only for pt in patches)
                else ""
            )
            self._header(
                Text(
                    _(
                        "Repo má necommitnuté změny (~{dirty}) – opravy souborů se nespustí, repo-doctor na ně nesahá. Commitni je nebo ulož do stashe.{local}"
                    ).format(dirty=dirty, local=local),
                    style=Style(color=p.med),
                )
            )
        else:
            self._header(
                Text(
                    _("Vyber, co opravit. Změny vzniknou v nové větvi, nic se nepushne."),
                    style=Style(color=p.text),
                )
            )
        self._render_list()
        self.query_one("#rx", OptionList).focus()

    def _render_list(self) -> None:
        """Předpis: zaškrtávací položky o dvou řádcích (ID kontroly + co se změní)."""
        p = self.rd.palette
        lst = self.query_one("#rx", OptionList)
        if self.selected is None:
            # na repu s necommitnutými změnami nic nepředvybíráme – oprava se stejně nespustí
            chosen: dict[str, str] = {}
            if not self.dirty:
                for pt in self.patches:
                    if self.focus_check in (None, pt.check_id):
                        chosen.setdefault(pt.check_id, pt.key)  # první varianta
            self.selected = set(chosen.values())
        highlighted = lst.highlighted
        lst.clear_options()
        width = max(20, lst.size.width - 6) if lst.size.width else 60
        for patch in self.patches:
            on_ = patch.key in self.selected
            variant = any(o.check_id == patch.check_id and o is not patch for o in self.patches)
            box = Text(
                ("(•) " if on_ else "( ) ") if variant else ("[x] " if on_ else "[ ] "),
                style=Style(color=p.ok if on_ else p.muted, bold=on_),
            )
            box.append(patch.check_id, style=Style(color=p.text, bold=True))
            if patch.variant:
                box.append(f" · {patch.title}", style=Style(color=p.accent))
            box.append("\n    " + patch.summary[:width], style=Style(color=p.muted))
            lst.add_option(Option(box, id=patch.key))
        if highlighted is not None and highlighted < len(self.patches):
            lst.highlighted = highlighted
        elif self.focus_check and self.focus_check in [pt.check_id for pt in self.patches]:
            lst.highlighted = [pt.check_id for pt in self.patches].index(self.focus_check)
        elif self.patches:
            lst.highlighted = 0
        self._summary()
        self._render_diff()
        bar = self.query_one(KeyBar)
        keys: list[str | tuple[str, str]] = ["toggle_fix", "select_all", ("j/k", _("pohyb"))]
        if self.patches and (not self.dirty or any(pt.local_only for pt in self.patches)):
            keys.append("confirm_fix")
        if self.patches and not self.dirty:
            keys.append("fix_publish")
        bar.set_items([*keys, ("esc", _("zpět")), "help"])

    def _summary(self) -> None:
        p = self.rd.palette
        n = len(self.selected or ())
        t = Text(
            _("vybráno {n} · {n} {commits_word}\n").format(n=n, commits_word=commits_word(n)),
            style=Style(color=p.text),
        )
        t.append(_("větev {branch}").format(branch=self.branch), style=Style(color=p.accent))
        self.query_one("#rx-summary", Static).update(t)

    def _render_diff(self) -> None:
        if not self.is_mounted:
            return
        panel = self.query_one("#diff-panel")
        patch = self.current or (self.patches[0] if self.patches else None)
        if patch is None:
            self.query_one("#diff", Static).update("")
            return
        files = ", ".join(
            [c.path for c in patch.changes[:3]]
            + [
                f".git/{'config' if a.kind == 'git_config' else 'hooks/' + a.key}"
                for a in patch.local
            ]
        )
        panel.border_title = _(" NÁHLED ZMĚN · {files} ").format(files=files)
        width = max(20, panel.size.width - 4)
        text = diff_text(patch.diff(), self.rd.palette, width)
        if patch.notes:
            text.append("\n")
            for note in patch.notes:
                text.append(f"! {note}\n", style=Style(color=self.rd.palette.med))
        self.query_one("#diff", Static).update(text)

    @on(OptionList.OptionHighlighted, "#rx")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        key = event.option.id
        self.current = next((pt for pt in self.patches if pt.key == key), None)
        self._render_diff()

    @on(OptionList.OptionSelected, "#rx")
    def _selected_option(self, event: OptionList.OptionSelected) -> None:
        self.action_toggle_fix()

    def action_toggle_fix(self) -> None:
        lst = self.query_one("#rx", OptionList)
        if lst.highlighted is None or self.selected is None or not self.patches:
            return
        patch = self.patches[lst.highlighted]
        if patch.key in self.selected:
            self.selected.discard(patch.key)
        else:
            # varianty jedné kontroly se vylučují
            self.selected -= {o.key for o in self.patches if o.check_id == patch.check_id}
            self.selected.add(patch.key)
        self._render_list()

    def action_select_all(self) -> None:
        if self.selected is None:
            return
        firsts: dict[str, str] = {}
        for pt in self.patches:
            firsts.setdefault(pt.check_id, pt.key)
        ids = set(firsts.values())
        self.selected = set() if self.selected >= ids else ids
        self._render_list()

    def action_confirm_fix(self) -> None:
        if not self.ready:
            return
        chosen = [pt for pt in self.patches if pt.key in (self.selected or set())]
        if self.dirty and any(pt.changes for pt in chosen):
            self.notify(
                _("Repo má necommitnuté změny – opravy souborů se nespustí."), severity="error"
            )
            return
        if not chosen:
            self.notify(
                _("Nic není vybráno ({key} vybere, {key2} vše).").format(
                    key=self.rd.key("toggle_fix"), key2=self.rd.key("select_all")
                ),
                severity="warning",
            )
            return
        repo = self.repo
        default = (repo.state.default_branch or repo.state.branch or "main") if repo else "main"
        n = len([pt for pt in chosen if pt.changes])
        body = Text()
        if n:
            body.append(_("\nVznikne nová větev\n  "))
            body.append(self.branch, style=Style(color=self.rd.palette.accent, bold=True))
            body.append(
                _(
                    "\nse {n} {commits_word}. Nic se nepushne, {default} zůstane\nbeze změny a necommitnuté soubory nedotčené.\n"
                ).format(n=n, commits_word=commits_word(n), default=default)
            )
        local = [a.description for pt in chosen for a in pt.local]
        if local:
            body.append(_("\nJen lokálně v .git/ (nic se necommituje):\n"))
            for desc in local:
                body.append(f"  • {desc}\n")

        def decided(ok: bool | None) -> None:
            if ok:
                self.do_apply(chosen)
            else:
                self.notify(_("Léčba zrušena – nic se nezměnilo."))

        self.app.push_screen(
            ConfirmDialog(_("Potvrdit léčbu"), body, confirm_label=_("Aplikovat")), decided
        )

    def action_fix_publish(self) -> None:
        """Vyléčit vybrané a na pozadí dotáhnout push → PR → CI → merge (#70)."""
        if not self.ready:
            return
        chosen = [pt for pt in self.patches if pt.key in (self.selected or set())]
        if not chosen:
            self.notify(
                _("Nic není vybráno ({key} vybere, {key2} vše).").format(
                    key=self.rd.key("toggle_fix"), key2=self.rd.key("select_all")
                ),
                severity="warning",
            )
            return
        if self.dirty:
            self.notify(
                _("Repo má necommitnuté změny – opravy se nespustí, workflow nejde zahájit."),
                severity="error",
            )
            return
        if self.rd.offline:
            self.notify(
                _("Workflow potřebuje hosting – vypni offline režim ({key}).").format(
                    key=self.rd.key("toggle_offline")
                ),
                severity="warning",
            )
            return
        if not any(pt.changes for pt in chosen):
            self.notify(
                _("Vybrané opravy mění jen .git/ – není co odesílat, použij {key}.").format(
                    key=self.rd.key("confirm_fix")
                ),
                severity="warning",
            )
            return
        repo = self.repo
        default = (repo.state.default_branch or repo.state.branch or "main") if repo else "main"
        n = len([pt for pt in chosen if pt.changes])
        body = Text()
        body.append(_("\nNa pozadí proběhne postupně:\n"))
        body.append(
            _("  1. nová větev {branch} se {n} {commits_word}\n").format(
                branch=self.branch, n=n, commits_word=commits_word(n)
            )
        )
        body.append(_("  2. push jen této větve (nikdy force)\n"))
        body.append(_("  3. pull request do {default}\n").format(default=default))
        body.append(_("  4. čekání na CI\n"))
        body.append(
            _(
                "  5. po ZELENÉ CI merge na hostingu (repo-doctor ho nevrací, jde jen revert commitem)\n"
            )
        )
        body.append(
            _(
                "\nPři chybě CI, konfliktu nebo odmítnutí hostingu se workflow zastaví a upozorní tě\nv Notifikacích na přehledu. Větev i PR zůstanou k ručnímu dokončení.\n"
            )
        )

        def decided(ok: bool | None) -> None:
            if not ok:
                self.notify(_("Zrušeno – nic se nezměnilo."))
                return
            if self.rd.workflows.start_heal(self.path, chosen):
                self.notify(
                    _("Workflow běží na pozadí. Průběh je v Notifikacích na přehledu."),
                    title=_("Léčba a odeslání"),
                )
                self.app.pop_screen()

        self.app.push_screen(
            ConfirmDialog(
                _("Vyléčit a dotáhnout do mergi"), body, confirm_label=_("Spustit workflow")
            ),
            decided,
        )

    @work(thread=True, exclusive=True, group="fix-apply")
    def do_apply(self, chosen: list[Patch]) -> None:
        repo = self.repo
        if repo is None:
            return
        try:
            result = apply(
                Git(Path(repo.path)),
                chosen,
                today=self.rd.now().date() if self.rd.now() else date.today(),
            )
        except (FixRefused, GitError) as err:
            message = str(err) if isinstance(err, FixRefused) else f"git selhal: {err.stderr}"
            self.app.call_from_thread(
                self.notify, message, title=_("Léčba se nepovedla"), severity="error", timeout=10
            )
            return
        self.app.call_from_thread(self._applied, result)

    def _applied(self, result: ApplyResult) -> None:
        n = len(result.commits)
        parts = []
        if result.branch:
            parts.append(
                _(
                    "Vytvořena větev {branch} s {n} {commits_word}. Nic se nepushnulo – zkontroluj ji: git log {branch}. Odeslat ji jde na stránce Odeslání oprav (z karty repa klávesou {key})."
                ).format(
                    branch=result.branch,
                    n=n,
                    commits_word=commits_word(n),
                    key=self.rd.key("publish_open"),
                )
            )
        parts.extend(f"✓ {desc}" for desc in result.local)
        msg = "\n".join(parts)
        for note in result.notes:
            msg += f"\n! {note}"
        self.notify(msg, title=_("Léčba hotová"), timeout=15)
        self.app.pop_screen()
