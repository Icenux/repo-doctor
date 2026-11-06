"""Obrazovka Přehled / Triáž."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from repo_doctor.czech import plural
from repo_doctor.i18n import _
from repo_doctor.models import Category, RepoResult, Severity
from repo_doctor.scoring import Band
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.screens.base import NavMixin
from repo_doctor.tui.triage_model import (
    GROUP_LABELS,
    SORT_COLUMNS,
    SORT_LABELS,
    TriageState,
    build_groups,
    forge_key,
)
from repo_doctor.tui.util import home_path, pad
from repo_doctor.tui.widgets.keybar import KeyBar
from repo_doctor.tui.widgets.panel import split_title
from repo_doctor.tui.widgets.triage import (
    Columns,
    group_header,
    header_text,
    preview_text,
    repo_row,
)
from repo_doctor.tui.widgets.vitals import Vitals, vitals_text

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp

SIDEBAR_W = 22
SIDEBAR_MIN_WIDTH = 100  # užší terminál levý panel schová (jinak by se nevešla tabulka)
NOTIFICATIONS_SHOWN = 5  # kolik posledních událostí léčby ukáže levý panel
MIN_NAME_W = 13  # nejdelší názvy ve wireframu („thesis-2019“, „infra-notes“) se vejdou celé
SCAN_ANIMATION_INTERVAL = 0.12  # s mezi snímky animace pruhu skenu
# krátké tvary pro lištu; plné popisky jsou v nápovědě a paletě
DASH_KEYS: list[str | tuple[str, str]] = [
    ("scan_all", _("sken všech rep")),
    ("scan_repo", _("sken repa")),
    ("search", _("hledat")),
    ("open_card", _("karta")),
    ("open_fix", _("léčba")),
    ("collapse_band", _("sbalit")),
    ("screen_folders", _("složky")),
    ("screen_forges", "hostingy"),
    ("screen_settings", _("nastavení")),
    "help",
    ("quit", _("konec")),
]


class SideBar(OptionList):
    DEFAULT_CSS = """
    SideBar { width: 22; border: round $rd-border; padding: 0 0; background: $background; scrollbar-size-vertical: 0; }
    SideBar:focus { border: round $accent; }
    """


class TriageList(OptionList):
    DEFAULT_CSS = """
    TriageList { border: none; padding: 0; height: 1fr; background: $background; scrollbar-size-vertical: 1; }
    TriageList:focus { border: none; }
    """


class DashboardScreen(NavMixin, Screen[Any]):
    help_context = "dashboard"
    BINDINGS = [
        *bindings("nav"),
        *bindings("dashboard"),
        Binding("escape", "escape", _("zrušit hledání"), show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state = TriageState()
        self._row_paths: list[str | None] = []
        self._row_groups: list[str | None] = []
        self._scan_frame = 0

    @property
    def rd(self) -> RepoDoctorApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Vitals(id="vitals")
        with Horizontal(id="body"):
            yield SideBar(id="sidebar")
            with Vertical(id="triage-panel"):
                yield Input(
                    compact=True,
                    placeholder=_("hledat v názvech repozitářů a nálezech…"),
                    id="search",
                    classes="hidden",
                )
                yield Static(id="triage-header")
                yield TriageList(id="triage")
                yield Static(id="preview")
        yield KeyBar(DASH_KEYS)

    def on_mount(self) -> None:
        self.state.group_by = self.rd.config.ui.group_by
        self.refresh_data()
        self.query_one(TriageList).focus()
        self.set_interval(SCAN_ANIMATION_INTERVAL, self._animate_scan)

    def _animate_scan(self) -> None:
        if self.rd.scanning:
            self._scan_frame += 1
            self.refresh_data(light=True)

    def on_resize(self, event: Resize) -> None:
        self.refresh_data()

    def on_config_changed(self) -> None:
        self.refresh_data()

    def on_screen_resume(self) -> None:
        self.refresh_data()

    # ------------------------------------------------------------------ vykreslení
    @property
    def width(self) -> int:
        return self.app.size.width

    def _sidebar_visible(self) -> bool:
        return self.width >= SIDEBAR_MIN_WIDTH

    def refresh_data(self, *, light: bool = False) -> None:
        if not self.is_mounted:
            return
        app = self.rd
        p = app.palette
        limits = app.config.limits
        vitals = self.query_one(Vitals)
        forges = len(app.config.forges)
        mode = _("○ offline") if app.offline else _("● online")
        when = app.result.finished_at
        right = (
            _("{mode} · sken {astimezone:%H:%M}").format(mode=mode, astimezone=when.astimezone())
            if when
            else mode
        )
        right += f" · {forges} {plural(forges, _('hosting'), _('hostingy'), _('hostingů'))}"
        split_title(vitals, _("repo-doctor · triáž"), right)
        vitals.update(
            vitals_text(
                app.result,
                p,
                max(20, self.width - 4),
                no_pulse_days=limits.no_pulse_days,
                scanning=app.scanning,
                done=app.scan_done,
                total=app.scan_total,
                current=app.scan_current,
                keys=(app.key("scan_all"), app.key("cancel_scan")),
                scheduled=app.scheduled,
                frame=self._scan_frame,
            )
        )
        if light:
            return
        sidebar = self.query_one(SideBar)
        sidebar.display = self._sidebar_visible()
        panel = self.query_one("#triage-panel")
        sort = _("řazeno: {v} {v2}").format(
            v=SORT_LABELS[self.state.sort], v2="↓" if self.state.reverse else "↑"
        )
        split_title(
            panel,
            _("TRIÁŽ")
            if self.state.group_by == "triage"
            else _("PŘEHLED · {v}").format(v=GROUP_LABELS[self.state.group_by]),
            sort,
        )
        if sidebar.display:
            self._render_sidebar()
        self._render_triage()

    def _columns(self) -> Columns:
        panel_w = self.width - (SIDEBAR_W if self._sidebar_visible() else 0) - 4
        cols = Columns(width=panel_w, show_pulse=self.width >= 90, show_host=self.width >= 80)
        # Rozhoduje i skutečné místo v panelu: když je levý panel vedle tabulky,
        # obětujeme nejdřív TEP, pak HOSTING, aby se nezkracovaly názvy repozitářů.
        if cols.show_pulse and cols.name_w < MIN_NAME_W:
            cols = Columns(width=panel_w, show_pulse=False, show_host=cols.show_host)
        if cols.show_host and cols.name_w < MIN_NAME_W:
            cols = Columns(width=panel_w, show_pulse=False, show_host=False)
        return cols

    def _render_triage(self) -> None:
        app = self.rd
        p = app.palette
        cols = self._columns()
        self.query_one("#triage-header", Static).update(header_text(cols, p))
        lst = self.query_one(TriageList)
        previous = app.selected_path
        prev_index = lst.highlighted
        groups = build_groups(
            app.result, self.state, no_pulse_days=app.config.limits.no_pulse_days, now=app.now()
        )
        options: list[Option] = []
        self._row_paths, self._row_groups = [], []
        key = app.keymap_ids.get("collapse_band", "z")
        for g in groups:
            collapsed = g.key in self.state.collapsed
            options.append(
                Option(
                    group_header(g, cols.width, p, collapsed=collapsed, toggle_key=key),
                    id=f"g:{g.key}",
                )
            )
            self._row_paths.append(None)
            self._row_groups.append(g.key)
            if not collapsed:
                for r in g.repos:
                    options.append(Option(repo_row(r, cols, p), id=f"r:{r.path}"))
                    self._row_paths.append(r.path)
                    self._row_groups.append(g.key)
            options.append(Option("", disabled=True))
            self._row_paths.append(None)
            self._row_groups.append(None)
        if not options:
            msg = (
                _("Žádná repa neodpovídají filtru.")
                if self.state.filters.active()
                else _(
                    "Zatím žádný sken – stiskni {key} a repo-doctor zkontroluje všechna repa ve sledovaných složkách."
                ).format(key=app.key("scan_all"))
            )
            options.append(Option(Text(f"  {msg}", style=Style(color=p.muted)), disabled=True))
            self._row_paths.append(None)
            self._row_groups.append(None)
        lst.clear_options()
        lst.add_options(options)
        target = None
        if previous and previous in self._row_paths:
            target = self._row_paths.index(previous)
        elif (
            prev_index is not None
            and prev_index < len(options)
            and not options[prev_index].disabled
        ):
            target = prev_index
        else:
            target = next((i for i, path in enumerate(self._row_paths) if path), None)
            if target is None:
                target = next((i for i, o in enumerate(options) if not o.disabled), None)
        if target is not None:
            lst.highlighted = target
        visible = [p for p in self._row_paths if p]
        if app.selected_path not in visible:
            # vybrané repo zmizelo z pohledu (filtr, hledání) – nesmí zůstat skrytě vybrané
            hl = lst.highlighted
            app.selected_path = (
                self._row_paths[hl] if hl is not None and hl < len(self._row_paths) else None
            )
            if app.selected_path is None and visible:
                app.selected_path = visible[0]
        self._update_preview()

    def _update_preview(self) -> None:
        app = self.rd
        width = self._columns().width
        self.query_one("#preview", Static).update(
            preview_text(app.selected_repo, width, app.palette)
        )

    def _render_sidebar(self) -> None:
        app = self.rd
        p = app.palette
        f = self.state.filters
        sb = self.query_one(SideBar)
        w = SIDEBAR_W - 3
        hl = sb.highlighted
        opts: list[Option] = []

        def head(text: str) -> None:
            opts.append(Option(Text(text, style=Style(color=p.accent, bold=True)), disabled=True))

        def item(
            label: str,
            count: str,
            oid: str,
            *,
            active: bool = False,
            dim: bool = False,
            mark: bool = True,
        ) -> None:
            left = (("▸ " if active else "  ") if mark else "") + label
            t = Text(
                pad(left, w - len(count)).plain,
                style=Style(color=p.dim if dim else p.text, bold=active),
            )
            t.append(count, style=Style(color=p.muted))
            opts.append(Option(t, id=oid))

        head(_("SLOŽKY"))
        roots = app.roots
        for root in roots:
            n = sum(1 for r in app.result.repos if r.root == root.path)
            name = home_path(str(root.expanded)).rstrip("/").rsplit("/", 1)[-1] or root.path
            item(
                name,
                f"{n}" if root.enabled else _("vyp."),
                f"root:{root.path}",
                active=f.root == root.path,
                dim=not root.enabled,
            )
        if not roots:
            opts.append(
                Option(
                    Text(
                        _("  žádné ({key} přidá)").format(key=app.key("screen_folders")),
                        style=Style(color=p.muted),
                    ),
                    disabled=True,
                )
            )
        opts.append(Option("", disabled=True))
        self._notification_block(opts, head, w)
        head(_("HOSTINGY"))
        counts: dict[str, int] = {}
        for r in app.result.repos:
            counts[forge_key(r)] = counts.get(forge_key(r), 0) + 1
        connected = {fc.name for fc in app.config.forges}
        for name in sorted(k for k in counts if k != "-"):
            sym = "◆" if name in connected else "·"
            item(f"{sym} {name}", str(counts[name]), f"forge:{name}", active=f.forge == name)
        if "-" in counts:
            item(_("○ bez remote"), str(counts["-"]), "forge:-", active=f.forge == "-")
        opts.append(Option("", disabled=True))
        head(_("FILTR"))
        item(("■" if f.problems_only else "□") + _(" jen problémová"), "", "f:problems", mark=False)
        item(
            ("■" if self.state.group_by != "triage" else "□")
            + _(" seskupit: ")
            + GROUP_LABELS[self.state.group_by],
            "",
            "f:group",
            mark=False,
        )
        sev = (
            _("severity ≥ {short}").format(short=f.min_severity.short)
            if f.min_severity
            else _("severity: vše")
        )
        item("  " + sev, "", "f:severity", active=f.min_severity is not None, mark=False)
        cat = (
            _("kat.: {label}").format(label=f.category.label) if f.category else _("kategorie: vše")
        )
        item("  " + cat, "", "f:category", active=f.category is not None, mark=False)
        opts.append(Option("", disabled=True))
        head(_("OD VČEREJŠKA"))
        delta = app.history_delta
        if not delta:
            opts.append(Option(Text(_("  beze změny"), style=Style(color=p.muted)), disabled=True))
        labels = {"high": "HIGH", "medium": "MED", "low": "LOW", "no_pulse": _("bez tepu")}
        for key in ("high", "medium", "low", "no_pulse"):
            if key in delta:
                v = delta[key]
                color = (p.high if v > 0 else p.ok) if key != "no_pulse" else p.muted
                sign = "+" if v > 0 else "−"
                opts.append(
                    Option(
                        Text(f"  {sign}{abs(v)} {labels[key]}", style=Style(color=color)),
                        disabled=True,
                    )
                )
        sb.clear_options()
        sb.add_options(opts)
        if hl is not None and hl < len(opts) and not opts[hl].disabled:
            sb.highlighted = hl

    def _notification_block(
        self, opts: list[Option], head: Callable[[str], None], width: int
    ) -> None:
        """NOTIFIKACE pod složkami: běžící workflow a posledních pár událostí léčby (#70)."""
        runner = self.rd.workflows
        p = self.rd.palette
        unread = runner.inbox.unread()
        shown_entries = runner.inbox.entries[:NOTIFICATIONS_SHOWN]
        if not runner.live and not shown_entries:
            return
        head(_("NOTIFIKACE") + (f" ({unread})" if unread else ""))

        def line(text: str, color: str, oid: str, *, bold: bool = False) -> None:
            opts.append(
                Option(Text(pad(text, width).plain, style=Style(color=color, bold=bold)), id=oid)
            )

        for path in runner.live:
            line(f"⟳ {runner.repo_name(path)}", p.accent, f"n:live:{path}", bold=True)
        colors = {"ok": p.ok, "warn": p.med, "error": p.high, "info": p.text}
        marks = {"ok": "✓", "warn": "!", "error": "✗", "info": "·"}
        for e in shown_entries:
            mark = marks.get(e.level, "·")
            line(
                f"{'●' if not e.read else ' '}{mark} {e.repo_name}",
                colors.get(e.level, p.text) if not e.read else p.muted,
                f"n:{e.id}",
                bold=not e.read,
            )
        if unread:
            opts.append(
                Option(
                    Text(
                        _("  vše přečteno ({key})").format(key=self.rd.key("inbox_read")),
                        style=Style(color=p.muted),
                    ),
                    id="n:all",
                )
            )
        opts.append(Option("", disabled=True))

    def _open_notification(self, oid: str) -> None:
        runner = self.rd.workflows
        if oid == "n:all":
            runner.inbox.mark_read()
            self.refresh_data()
            return
        if oid.startswith("n:live:"):
            path = oid[7:]
        else:
            entry = runner.inbox.get(oid[2:])
            if entry is None:
                return
            runner.inbox.mark_read(entry.id)
            path = entry.repo_path
            self.refresh_data()
        if self.rd.repo(path) is None:
            self.notify(
                _("Repo už není ve výsledcích skenu – spusť sken ({key}).").format(
                    key=self.rd.key("scan_all")
                ),
                severity="warning",
            )
            return
        self.rd.open_publish(path)

    def action_inbox_read(self) -> None:
        self.rd.workflows.inbox.mark_read()
        self.refresh_data()

    # ------------------------------------------------------------------ události
    @on(OptionList.OptionHighlighted, "#triage")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._row_paths) and self._row_paths[idx]:
            self.rd.selected_path = self._row_paths[idx]
            self._update_preview()

    @on(OptionList.OptionSelected, "#triage")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        path = self._row_paths[idx] if 0 <= idx < len(self._row_paths) else None
        if path:
            self.rd.open_detail(path)
        elif 0 <= idx < len(self._row_groups):
            group = self._row_groups[idx]
            if group:
                self._toggle_group(group)

    @on(OptionList.OptionSelected, "#sidebar")
    def _sidebar_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id or ""
        f = self.state.filters
        if oid.startswith("n:"):
            self._open_notification(oid)
            return
        if oid.startswith("root:"):
            value = oid[5:]
            f.root = None if f.root == value else value
        elif oid.startswith("forge:"):
            value = oid[6:]
            f.forge = None if f.forge == value else value
        elif oid == "f:problems":
            self.action_problems_only()
            return
        elif oid == "f:group":
            self.action_group_cycle()
            return
        elif oid == "f:severity":
            order = [None, Severity.HIGH, Severity.MEDIUM, Severity.LOW]
            f.min_severity = order[(order.index(f.min_severity) + 1) % len(order)]
        elif oid == "f:category":
            cats: list[Category | None] = [None, *Category]
            f.category = cats[(cats.index(f.category) + 1) % len(cats)]
        self.refresh_data()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.state.filters.query = event.value.strip()
        self._render_triage()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.query_one(TriageList).focus()

    # ------------------------------------------------------------------ akce
    def open_search(self) -> None:
        search = self.query_one("#search", Input)
        search.remove_class("hidden")
        search.focus()

    def action_escape(self) -> None:
        search = self.query_one("#search", Input)
        if not search.has_class("hidden") or self.state.filters.query:
            search.value = ""
            self.state.filters.query = ""
            search.add_class("hidden")
            self.query_one(TriageList).focus()
            self._render_triage()

    def _toggle_group(self, key: str) -> None:
        if key in self.state.collapsed:
            self.state.collapsed.discard(key)
        else:
            self.state.collapsed.add(key)
        self._render_triage()
        lst = self.query_one(TriageList)
        if f"g:{key}" in [o.id for o in lst.options]:
            lst.highlighted = [o.id for o in lst.options].index(f"g:{key}")

    def action_collapse_band(self) -> None:
        idx = self.query_one(TriageList).highlighted
        if idx is not None and idx < len(self._row_groups) and self._row_groups[idx]:
            self._toggle_group(self._row_groups[idx])  # type: ignore[arg-type]
        elif Band.HEALTHY.value in self.state.collapsed:
            self._toggle_group(Band.HEALTHY.value)

    def action_open_card(self) -> None:
        if self.rd.selected_repo is not None:
            self.rd.open_detail(self.rd.selected_repo.path)

    def action_open_fix(self) -> None:
        repo = self.rd.selected_repo
        if repo is None:
            self.notify(_("Nejdřív vyber repo."), severity="warning")
            return
        self.rd.open_fix(repo.path)

    def action_sort_cycle(self) -> None:
        i = SORT_COLUMNS.index(self.state.sort)
        self.state.sort = SORT_COLUMNS[(i + 1) % len(SORT_COLUMNS)]
        self.state.reverse = False
        self.refresh_data()

    def action_sort_reverse(self) -> None:
        self.state.reverse = not self.state.reverse
        self.refresh_data()

    def _severity_filter(self, sev: Severity) -> None:
        f = self.state.filters
        f.min_severity = None if f.min_severity is sev else sev
        self.refresh_data()

    def action_filter_high(self) -> None:
        self._severity_filter(Severity.HIGH)

    def action_filter_medium(self) -> None:
        self._severity_filter(Severity.MEDIUM)

    def action_filter_low(self) -> None:
        self._severity_filter(Severity.LOW)

    def action_problems_only(self) -> None:
        self.state.filters.problems_only = not self.state.filters.problems_only
        self.refresh_data()

    def action_group_cycle(self) -> None:
        order = ["triage", "root", "forge", "remote"]
        self.state.group_by = order[(order.index(self.state.group_by) + 1) % len(order)]  # type: ignore[assignment]
        self.refresh_data()

    def set_category(self, category: Category | None) -> None:
        self.state.filters.category = category
        self.refresh_data()

    def select_repo(self, path: str) -> None:
        self.rd.selected_path = path
        self.state.filters = type(self.state.filters)()
        self.state.collapsed.clear()
        self.refresh_data()

    def visible_repos(self) -> list[RepoResult]:
        return [r for p in self._row_paths if p and (r := self.rd.repo(p)) is not None]
