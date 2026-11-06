"""Nastavení: kontroly, limity, allowlist, téma, editor… Ukládá se okamžitě, když je hodnota platná."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, Select, SelectionList, Static
from textual.widgets.selection_list import Selection

from repo_doctor import schedule
from repo_doctor.checks import all_checks
from repo_doctor.config import ConfigError, LimitsConfig
from repo_doctor.i18n import _
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.screens.base import BaseScreen
from repo_doctor.tui.screens.dialogs import ConfirmDialog
from repo_doctor.tui.util import available_editors, editor_installed, home_path, resolve_editor
from repo_doctor.tui.widgets.forms import IntRange, PositiveNumber, split_list
from repo_doctor.tui.widgets.keybar import KeyBar

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp

EDITOR_AUTO = "__auto__"
EDITOR_CUSTOM = "__custom__"

LIMIT_LABELS: dict[str, str] = {
    "large_file_mb": _("velký soubor (MB)"),
    "binary_file_kb": _("binárka mimo LFS (KB)"),
    "stale_branch_days": _("stará branch (dní)"),
    "stash_days": _("zapomenutý stash (dní)"),
    "no_pulse_days": _("bez tepu (dní)"),
    "stale_pr_days": _("neaktivní PR (dní)"),
    "history_timeout_s": _("limit skenu historie (s)"),
    "git_timeout_s": _("limit příkazu git (s)"),
    "history_size": _("historie skenů (počet)"),
    "http_cache_minutes": _("cache API hostingů (min)"),
    "registry_cache_hours": _("cache registrů a OSV (h)"),
    "max_file_kb": _("max. soubor pro sken tajemství (KB)"),
}
FLOAT_LIMITS = {"large_file_mb", "history_timeout_s", "git_timeout_s"}


class SettingsScreen(BaseScreen):
    help_context = "settings"
    BINDINGS = [*BaseScreen.BINDINGS, *bindings("settings")]

    @property
    def rd(self) -> RepoDoctorApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        cfg = self.rd.config
        with VerticalScroll(classes="page panel form", id="settings") as page:
            page.border_title = _(" nastavení ")
            yield Static(
                Text(
                    _("Změny se ukládají okamžitě do {home_path}").format(
                        home_path=home_path(str(self.rd.store.path))
                    ),
                    style=Style(color=self.rd.palette.muted),
                )
            )
            yield Label(_("Plánovaný sken"))
            yield Static(_("zjišťuji stav…"), id="schedule-status", classes="hint")
            with Horizontal(classes="row"):
                yield Select(
                    compact=True,
                    options=[(_("týdně (pondělí)"), "weekly"), (_("denně"), "daily")],
                    value="weekly",
                    allow_blank=False,
                    id="schedule-interval",
                )
                yield Input(compact=True, value="08:00", id="schedule-time", classes="short")
            with Horizontal(classes="row"):
                yield Button(_("Zapnout"), id="schedule-install", compact=True)
                yield Button(_("Vypnout"), id="schedule-remove", compact=True)
                yield Button(_("Test upozornění"), id="schedule-test", compact=True)
            yield Label(_("Sken po spuštění aplikace"))
            yield Select(
                compact=True,
                options=[(_("vypnutý – ukázat poslední výsledek"), "off"), (_("zapnutý"), "on")],
                value="on" if cfg.ui.scan_on_start else "off",
                allow_blank=False,
                id="scan_on_start",
            )
            yield Label(_("Jazyk / Language"))
            yield Select(
                compact=True,
                options=[
                    (_("automaticky podle systému"), "auto"),
                    (_("čeština"), "cs"),
                    ("English", "en"),
                ],
                value=cfg.ui.language,
                allow_blank=False,
                id="language",
            )
            yield Label(_("Editor"))
            yield Static(
                _(
                    "Klávesa {key} v kartě repa otevře soubor nálezu na jeho řádku – na ruční opravu toho, co nejde opravit automaticky."
                ).format(key=self.rd.key("open_editor")),
                classes="hint",
            )
            editors = available_editors()
            known = {cmd for cmd, _ in editors}
            choice = (
                EDITOR_AUTO
                if not cfg.ui.editor
                else cfg.ui.editor
                if cfg.ui.editor in known
                else EDITOR_CUSTOM
            )
            yield Select(
                compact=True,
                options=[
                    (_("automaticky ($VISUAL / $EDITOR)"), EDITOR_AUTO),
                    *[(label, cmd) for cmd, label in editors],
                    (_("vlastní příkaz…"), EDITOR_CUSTOM),
                ],
                value=choice,
                allow_blank=False,
                id="editor-choice",
            )
            editor_input = Input(
                compact=True,
                value=cfg.ui.editor if choice == EDITOR_CUSTOM else "",
                placeholder=_("např. code --wait nebo /opt/editor/bin/ed"),
                id="editor",
            )
            editor_input.display = choice == EDITOR_CUSTOM
            yield editor_input
            yield Static(id="editor-info", classes="hint")
            yield Label(_("Práh pro exit kód CLI (fail-on)"))
            yield Select(
                compact=True,
                options=[("▲ HIGH", "high"), ("◆ MEDIUM", "medium"), ("● LOW", "low")],
                value=cfg.checks.fail_on.value,
                allow_blank=False,
                id="fail_on",
            )
            yield Label(_("Licence pro opravu license-missing"))
            yield Select(
                compact=True,
                options=[(x, x) for x in ("MIT", "ISC", "BSD-2-Clause", "Unlicense")],
                value=cfg.license,
                allow_blank=False,
                id="license",
            )
            yield Label(_("Vlastní šablony oprav (složka, volitelné)"))
            yield Input(
                compact=True,
                value=cfg.templates_dir or "",
                placeholder="~/.config/repo-doctor/templates",
                id="templates_dir",
            )
            yield Label(_("Aktivní kontroly (space přepne)"))
            yield SelectionList[str](
                *[
                    Selection(f"{c.id}  · {_(c.title)}", c.id, c.id not in cfg.checks.disabled)
                    for c in all_checks()
                ],
                id="checks",
            )
            yield Label(_("Limity"))
            for key, label in LIMIT_LABELS.items():
                validator = (
                    PositiveNumber()
                    if key in FLOAT_LIMITS
                    else IntRange(0 if "cache" in key else 1, 1_000_000)
                )
                yield Static(label, classes="hint")
                yield Input(
                    compact=True,
                    value=str(getattr(cfg.limits, key)),
                    validators=[validator],
                    id=f"limit-{key}",
                )
            yield Label(_("Ignorované cesty v repech (glob, čárkami)"))
            yield Input(
                compact=True,
                value=", ".join(cfg.ignore_paths),
                placeholder="vendor/**, docs/examples/**",
                id="ignore_paths",
            )
            yield Label(_("Ignorované repozitáře (název nebo glob, čárkami)"))
            yield Input(compact=True, value=", ".join(cfg.ignore_repos), id="ignore_repos")
            yield Label(_("Hosty s povoleným nešifrovaným http (allow_http_hosts, čárkami)"))
            yield Static(
                _(
                    "Jen pro důvěryhodnou síť (Tailscale/VPN) – po http jde i token hostingu. "
                    "Hostname, *.domena, IP (např. 100.101.102.103) nebo síť CIDR, bez portu a bez http://."
                ),
                classes="hint",
            )
            yield Input(
                compact=True,
                value=", ".join(cfg.allow_http_hosts),
                placeholder="git.tailnet-name.ts.net, 100.64.0.0/10",
                id="allow_http_hosts",
            )
            yield Label(_("Volitelné nástroje"))
            yield Static(
                _(
                    "gitleaks, schránka, prohlížeč a upozornění spravuješ na obrazovce Nástroje (klávesa {key})."
                ).format(key=self.rd.key("screen_tools")),
                classes="hint",
                id="tools-hint",
            )
            yield Label(_("Allowlist nálezů (d odebere vybraný)"))
            yield DataTable(id="allowlist", cursor_type="row")
            yield Static("", classes="error", id="settings-error")
        yield KeyBar(
            [
                ("space", _("přepnout")),
                ("tab", _("další pole")),
                "settings_allow_remove",
                ("esc", _("zpět")),
                "help",
            ]
        )

    @work(thread=True, exclusive=True, group="schedule-status")
    def refresh_schedule(self) -> None:
        from repo_doctor import notify

        backend = self.rd.schedule_backend
        try:
            st = backend.status()
        except Exception as err:
            text = _("stav nejde zjistit: {name}").format(name=type(err).__name__)
        else:
            if st.installed:
                text = (
                    _("zapnutý") if st.active else _("nainstalovaný, ale neaktivní")
                ) + f" · {schedule.describe(st.schedule)}"
                parsed = schedule.parse_calendar(st.schedule)
                if parsed:
                    self.app.call_from_thread(self._show_schedule, *parsed)
                if st.next_run:
                    text += _(" · další běh {next_run}").format(next_run=st.next_run)
            elif not st.systemd:
                text = _(
                    "vypnutý · uživatelský systemd tu není (repo-doctor schedule install vypíše řádek pro cron)"
                )
            else:
                text = _("vypnutý")
        last = self.rd.scheduled or notify.load_state()
        if last is not None:
            text += _("\nposlední běh {replace} · {v}").format(
                replace=last.ts[:16].replace("T", " "), v=last.changes or _("beze změn")
            )
        self.app.call_from_thread(self.query_one("#schedule-status", Static).update, text)

    def _show_schedule(self, interval: str, time: str) -> None:
        """Pole ukazují skutečně nainstalovaný rozvrh, ne výchozí hodnoty."""
        self.query_one("#schedule-interval", Select).value = interval
        self.query_one("#schedule-time", Input).value = time

    @on(Button.Pressed, "#schedule-install")
    def _schedule_install(self) -> None:
        interval = str(self.query_one("#schedule-interval", Select).value)
        time = self.query_one("#schedule-time", Input).value.strip()
        try:
            when = schedule.on_calendar("weekly" if interval == "weekly" else "daily", time)
        except schedule.ScheduleError as err:
            self.notify(str(err), severity="error")
            return
        unit = schedule.unit_dir()
        body = Text(
            _(
                "\nZapíše uživatelský systemd timer (bez roota):\n  {home_path}\n  {home_path2}\n\nOnCalendar={when} ({describe})\nSpouští `repo-doctor scan --scheduled`; upozorní jen na změny.\n"
            ).format(
                home_path=home_path(str(unit / "repo-doctor.service")),
                home_path2=home_path(str(unit / "repo-doctor.timer")),
                when=when,
                describe=schedule.describe(when),
            )
        )

        def done(ok: bool | None) -> None:
            if ok:
                self.do_schedule("install", interval, time)

        self.app.push_screen(
            ConfirmDialog(_("Zapnout plánovaný sken"), body, confirm_label=_("Zapnout")), done
        )

    @on(Button.Pressed, "#schedule-remove")
    def _schedule_remove(self) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self.do_schedule("remove", "", "")

        self.app.push_screen(
            ConfirmDialog(
                _("Vypnout plánovaný sken"),
                Text(_("\nZastaví a smaže uživatelský systemd timer repo-doctor.\n")),
                confirm_label=_("Vypnout"),
            ),
            done,
        )

    @on(Button.Pressed, "#schedule-test")
    def _schedule_test(self) -> None:
        self.do_schedule("test", "", "")

    @work(thread=True, exclusive=True, group="schedule-action")
    def do_schedule(self, action: str, interval: str, time: str) -> None:
        from repo_doctor import notify

        backend = self.rd.schedule_backend
        severity = "information"
        try:
            if action == "install":
                backend.install(interval, time)
                message = _("Plánovaný sken zapnutý (uživatelský systemd timer).")
            elif action == "remove":
                message = (
                    _("Plánovaný sken vypnutý.")
                    if backend.remove()
                    else _("Plánovaný sken nebyl zapnutý.")
                )
            else:
                results = notify.deliver(self.rd.config.notify, _("Zkušební upozornění – funguje."))
                if not results:
                    message, severity = (
                        _("Není nastavený žádný kanál ([notify] v config.toml)."),
                        "warning",
                    )
                else:
                    message = " · ".join(
                        f"{'✓' if d.ok else '✗'} {d.channel} {d.detail}".strip() for d in results
                    )
                    severity = "information" if all(d.ok for d in results) else "warning"
        except backend.ScheduleError as err:
            message, severity = str(err), "error"
        self.app.call_from_thread(self.notify, message, severity=severity, timeout=12)
        self.refresh_schedule()

    def on_mount(self) -> None:
        table = self.query_one("#allowlist", DataTable)
        table.add_columns("hash", _("kontrola"), "repo", _("důvod"))
        self._fill_allowlist()
        self.refresh_schedule()
        self._show_editor()
        self.query_one("#language", Select).focus()

    def _show_editor(self) -> None:
        cmd, source = resolve_editor(self.rd.config.ui.editor)
        text = _("Spustí se: {cmd} (podle: {source})").format(cmd=cmd, source=source)
        if not editor_installed(cmd):
            text += _(" – ⚠ není nainstalovaný, vyber jiný")
        self.query_one("#editor-info", Static).update(text)

    def _fill_allowlist(self) -> None:
        table = self.query_one("#allowlist", DataTable)
        table.clear()
        for entry in self.rd.config.allowlist:
            table.add_row(
                entry.hash, entry.check or "—", entry.repo or "—", entry.reason, key=entry.hash
            )
        if not self.rd.config.allowlist:
            table.add_row("—", "", "", _("prázdný (nález skryješ klávesou a v kartě repa)"))

    def _save(self, fn: Any, *args: Any) -> bool:
        err = self.query_one("#settings-error", Static)
        try:
            config = fn(*args)
        except ConfigError as exc:
            err.update(str(exc))
            return False
        err.update("")
        self.rd.config_changed(config)
        return True

    @on(Select.Changed)
    def _select(self, event: Select.Changed) -> None:
        store = self.rd.store
        wid, value = event.select.id, event.value
        if wid == "language" and value != self.rd.config.ui.language:
            if self._save(store.set_value, "ui", "language", value):
                self.notify(_("Jazyk se změní po restartu aplikace."))
        elif wid == "scan_on_start" and (value == "on") != self.rd.config.ui.scan_on_start:
            self._save(store.set_value, "ui", "scan_on_start", value == "on")
        elif wid == "editor-choice":
            editor_input = self.query_one("#editor", Input)
            editor_input.display = value == EDITOR_CUSTOM
            if value == EDITOR_CUSTOM:
                editor_input.focus()
                return
            new = None if value == EDITOR_AUTO else str(value)
            if new != self.rd.config.ui.editor and self._save(store.set_value, "ui", "editor", new):
                self._show_editor()
        elif wid == "fail_on" and value != self.rd.config.checks.fail_on.value:
            self._save(store.set_value, "checks", "fail_on", value)
        elif wid == "license" and value != self.rd.config.license:
            self._save(store.set_top, "license", value)

    @on(Input.Changed)
    def _input(self, event: Input.Changed) -> None:
        wid = event.input.id or ""
        store = self.rd.store
        if event.validation_result is not None and not event.validation_result.is_valid:
            self.query_one("#settings-error", Static).update(
                "; ".join(event.validation_result.failure_descriptions)
            )
            return
        value = event.value.strip()
        if wid.startswith("limit-"):
            key = wid[6:]
            number: float | int = float(value) if key in FLOAT_LIMITS else int(value)
            if getattr(self.rd.config.limits, key) != number:
                self._save(store.set_value, "limits", key, number)
        elif wid == "editor" and value != (self.rd.config.ui.editor or ""):
            if self._save(store.set_value, "ui", "editor", value or None):
                self._show_editor()
        elif wid == "templates_dir" and value != (self.rd.config.templates_dir or ""):
            self._save(store.set_top, "templates_dir", value or None)
        elif wid in ("ignore_paths", "ignore_repos", "allow_http_hosts"):
            items = split_list(value)
            if items != getattr(self.rd.config, wid):
                self._save(store.set_top, wid, items or None)

    @on(SelectionList.SelectedChanged, "#checks")
    def _checks(self, event: SelectionList.SelectedChanged[str]) -> None:
        enabled = set(event.selection_list.selected)
        disabled = sorted(c.id for c in all_checks() if c.id not in enabled)
        if disabled != sorted(self.rd.config.checks.disabled):
            self._save(self.rd.store.set_value, "checks", "disabled", disabled or None)

    def action_settings_allow_remove(self) -> None:
        table = self.query_one("#allowlist", DataTable)
        if self.focused is not table:
            self.notify(_("Nejdřív přejdi (tab) na tabulku allowlistu."), severity="warning")
            return
        if not self.rd.config.allowlist or table.row_count == 0:
            return
        entry = self.rd.config.allowlist[min(table.cursor_row, len(self.rd.config.allowlist) - 1)]

        def done(ok: bool | None) -> None:
            if ok and self._save(self.rd.store.remove_allow, entry.hash):
                self._fill_allowlist()
                self.notify(
                    _(
                        "Odebráno z allowlistu – nález se znovu ukáže po dalším skenu ({check})."
                    ).format(check=entry.check)
                )

        self.app.push_screen(
            ConfirmDialog(
                _("Odebrat z allowlistu"),
                _("\nOdebrat výjimku {hash} ({v})?\nDůvod: {reason}\n").format(
                    hash=entry.hash, v=entry.check or "?", reason=entry.reason
                ),
                confirm_label=_("Odebrat"),
                danger=True,
            ),
            done,
        )


__all__ = ["LIMIT_LABELS", "LimitsConfig", "SettingsScreen"]
