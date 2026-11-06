"""Hlavní Textual aplikace repo-doctor."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.app import App
from textual.binding import Binding
from textual.worker import Worker, WorkerState

from repo_doctor import gitwrap, history, netpolicy, paths
from repo_doctor.config import Config, ConfigError, ConfigStore, RootConfig
from repo_doctor.gitwrap import configure_ssh
from repo_doctor.i18n import _
from repo_doctor.keymap import BY_ID, KeymapError, build_keymap, key_label, migrate_overrides
from repo_doctor.masking import install_log_redaction
from repo_doctor.models import RepoResult, ScanResult
from repo_doctor.scanner import ScanEvent, Scanner, ScanOptions
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.commands import RepoCommands
from repo_doctor.tui.theme import PALETTE, THEME, THEME_NAME, Palette

if TYPE_CHECKING:
    from rich.console import RenderableType

    from repo_doctor.tui.screens.dashboard import DashboardScreen

ScannerFactory = Callable[[Config, ScanOptions, Callable[[ScanEvent], None]], Scanner]
TERMINAL_WORKER_STATES = {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}


class RepoDoctorApp(App[int]):
    CSS_PATH = "app.tcss"
    TITLE = "repo-doctor"
    ENABLE_COMMAND_PALETTE = True
    COMMANDS = App.COMMANDS | {RepoCommands}
    BINDINGS = [
        *[b for b in bindings("global") if b.id not in {"command_palette", "quit"}],
        Binding(
            BY_ID["command_palette"].key,
            "command_palette",
            _("paleta"),
            show=False,
            priority=True,
            id="command_palette",
        ),
        Binding(BY_ID["quit"].key, "quit", _("konec"), show=False, priority=True, id="quit"),
    ]

    def __init__(
        self,
        *,
        store: ConfigStore | None = None,
        roots_override: list[str] | None = None,
        offline: bool = False,
        initial_result: ScanResult | None = None,
        auto_scan: bool = True,
        load_cache: bool = True,
        clock: Callable[[], datetime] | None = None,
        scanner_factory: ScannerFactory | None = None,
        history_delta: dict[str, int] | None = None,
        forge_transport: Any = None,
        schedule_backend: Any = None,
        update_checker: Callable[[], str | None] | None = None,
        tools_backend: Any = None,
        tools_hint: bool | None = None,
    ) -> None:
        super().__init__()
        from repo_doctor import schedule

        # install/remove/status plánovaného skenu (testy podstrčí náhradu bez systemd)
        self.schedule_backend: Any = schedule_backend or schedule
        install_log_redaction()
        gitwrap.reset_abort()  # předchozí instance mohla spouštění zastavit
        self.store = store or ConfigStore()
        self.config_error: str | None = None
        try:
            self.config = self.store.load()
        except ConfigError as err:
            self.config_error = str(err)
            self.config = Config()
        configure_ssh(batch_mode=self.config.ssh_batch_mode)
        netpolicy.configure(self.config.allow_http_hosts)
        self.roots_override = roots_override
        self.offline = offline
        self.auto_scan = auto_scan
        self.clock = clock or (lambda: datetime.now(UTC))
        self.scanner_factory = scanner_factory
        self.forge_transport = forge_transport
        self.result: ScanResult = initial_result or ScanResult()
        if initial_result is None and load_cache:
            cached = history.load_last_scan()
            if cached is not None:
                self.result = cached
        from repo_doctor import notify

        # poslední plánovaný běh (pro přehled); v testech se nečte
        self.scheduled: notify.RunState | None = notify.load_state() if load_cache else None
        from repo_doctor import update

        # upozornění na novou verzi (síť); v testech jen podstrčená náhrada
        self.update_checker = update_checker or (update.notice if load_cache else None)
        from repo_doctor import tools

        # stav a instalace volitelných nástrojů (testy podstrčí náhradu)
        self.tools_backend: Any = tools_backend or tools
        self.tools_hint = load_cache if tools_hint is None else tools_hint
        self.history_delta = (
            history_delta if history_delta is not None else history.previous_delta()
        )
        self.scanner: Scanner | None = None
        self.scan_worker: Worker[Any] | None = None
        self.scan_total = 0
        self.scan_done = 0
        self.scan_current = ""
        self.selected_path: str | None = None
        from repo_doctor.tui.workflow_runner import WorkflowRunner

        # workflow „vyléčit a dotáhnout do mergi“ (#70) a jeho notifikace
        self.workflows = WorkflowRunner(self)
        self.keymap_error: str | None = None
        self.keymap_ids: dict[str, str] = build_keymap({})
        self.register_theme(THEME)
        self.theme = THEME_NAME
        self._dashboard: DashboardScreen | None = None

    # ------------------------------------------------------------------ vlastnosti
    def key(self, action_id: str) -> str:
        return key_label(action_id, self.keymap_ids)

    @property
    def palette(self) -> Palette:
        return PALETTE

    @property
    def roots(self) -> list[RootConfig]:
        if self.roots_override:
            return [RootConfig(path=p, depth=3) for p in self.roots_override]
        return self.config.roots

    @property
    def scanning(self) -> bool:
        return self.scan_worker is not None and self.scan_worker.is_running

    def now(self) -> datetime:
        return self.clock()

    def repo(self, path: str | None) -> RepoResult | None:
        if path is None:
            return None
        return next((r for r in self.result.repos if r.path == path), None)

    @property
    def selected_repo(self) -> RepoResult | None:
        return self.repo(self.selected_path)

    # ------------------------------------------------------------------ start
    def on_mount(self) -> None:
        try:
            self.keymap_ids = build_keymap(self.config.keys)
            remapped = migrate_overrides(self.config.keys)
            overrides = {k: v for k, v in self.keymap_ids.items() if k in remapped}
            if overrides:
                self.set_keymap(overrides)
        except KeymapError as err:
            self.keymap_error = str(err)
            self.notify(
                str(err),
                title=_("Chyba v [keys] – používám výchozí zkratky"),
                severity="error",
                timeout=20,
            )
        if self.config_error:
            self.notify(
                self.config_error, title=_("Neplatná konfigurace"), severity="error", timeout=20
            )
        from repo_doctor.tui.screens.dashboard import DashboardScreen

        self._dashboard = DashboardScreen()
        self.push_screen(self._dashboard)
        if not self.store.exists and not self.roots_override and not self.config_error:
            from repo_doctor.tui.screens.wizard import WizardScreen

            self.push_screen(WizardScreen())
        elif self.auto_scan and self.config.ui.scan_on_start:
            self.start_scan()
        if self.update_checker is not None and self.config.updates.check and not self.offline:
            self.run_worker(self._check_update, thread=True, group="update", exit_on_error=False)
        if self.tools_hint:
            self.run_worker(self._hint_tools, thread=True, group="tools", exit_on_error=False)

    def _hint_tools(self) -> None:
        """Jednou upozorní, že sken tajemství by byl s gitleaks přesnější."""
        marker = paths.cache_dir() / "tools-hint"
        if marker.exists() or self.tools_backend.find("gitleaks") is not None:
            return
        self.call_from_thread(
            self.notify,
            _(
                "Sken tajemství bude rychlejší a přesnější s gitleaks – doinstaluješ ho na obrazovce Nástroje (klávesa {key})."
            ).format(key=self.key("screen_tools")),
            title=_("Tip"),
            timeout=12,
        )
        try:
            paths.ensure_private_dir(marker.parent)
            marker.touch()
        except OSError:
            pass

    def _check_update(self) -> None:
        if self.update_checker is None:  # pragma: no cover - hlídá on_mount
            return
        message = self.update_checker()
        if message:
            self.call_from_thread(self.notify, message, title=_("Nová verze"), timeout=15)

    @property
    def dashboard(self) -> DashboardScreen:
        if self._dashboard is None:  # pragma: no cover - vzniká v on_mount
            raise RuntimeError(_("dashboard ještě neexistuje"))
        return self._dashboard

    # ------------------------------------------------------------------ konfigurace
    def config_changed(self, config: Config) -> None:
        self.config = config
        configure_ssh(batch_mode=config.ssh_batch_mode)
        netpolicy.configure(config.allow_http_hosts)
        for screen in self.screen_stack:
            refresh = getattr(screen, "on_config_changed", None)
            if callable(refresh):
                refresh()

    # ------------------------------------------------------------------ sken
    def _make_scanner(self, options: ScanOptions) -> Scanner:
        if self.scanner_factory is not None:
            return self.scanner_factory(self.config, options, self._on_scan_event)
        net = None
        if not options.offline and self.forge_transport is not None:
            from repo_doctor.scanner import NetServices

            net = NetServices.create(self.config, transport=self.forge_transport)
        return Scanner(self.config, options, on_event=self._on_scan_event, net=net)

    def start_scan(self, repo_path: str | None = None, *, fetch: bool = False) -> None:
        if self.scanning:
            self.notify(
                _("Sken už běží ({key} ho zruší).").format(key=self.key("cancel_scan")),
                severity="warning",
            )
            return
        roots = self.roots
        if not roots:
            self.notify(
                _("Nejsou nastavené žádné složky – přidej je na obrazovce Složky ({key}).").format(
                    key=self.key("screen_folders")
                ),
                severity="warning",
            )
            return
        if fetch and self.offline:
            self.notify(
                _("Fetch potřebuje síť – vypni offline režim ({key}).").format(
                    key=self.key("toggle_offline")
                ),
                severity="warning",
            )
            return
        # fetch mění jen remote-tracking refy (stejně jako CLI `scan --fetch`)
        options = ScanOptions(offline=self.offline, now=None, fetch=fetch)
        self.scanner = self._make_scanner(options)
        self.scan_done, self.scan_total, self.scan_current = 0, 0, ""
        self._seen: set[str] = set()
        target = None
        if repo_path is not None:
            from repo_doctor.discovery import DiscoveredRepo

            existing = self.repo(repo_path)
            if existing is None:
                return
            target = [
                DiscoveredRepo(
                    path=Path(repo_path).resolve(), root=existing.root, display=repo_path
                )
            ]
        self.scan_worker = self.run_worker(
            self._scan(roots, target, full=repo_path is None, fetch=fetch),
            group="scan",
            exclusive=True,
        )
        self._refresh_views()

    async def _scan(
        self, roots: list[RootConfig], target: Any, *, full: bool, fetch: bool = False
    ) -> None:
        assert self.scanner is not None  # noqa: S101
        result = await self.scanner.run(roots, target)
        cancelled = result.cancelled or (self.scanner is not None and self.scanner.cancelled)
        if full and not cancelled:
            self.result = result
            try:
                history.save_last_scan(result)
                history.record(
                    result,
                    keep=self.config.limits.history_size,
                    no_pulse_days=self.config.limits.no_pulse_days,
                )
                self.history_delta = history.previous_delta()
            except OSError as err:  # pragma: no cover
                self.notify(
                    _("Výsledek skenu nejde uložit: {err}").format(err=err), severity="warning"
                )
        else:
            self.result.warnings = result.warnings or self.result.warnings
        self.result.offline = self.offline
        self.scan_current = ""
        self._refresh_views()
        if cancelled:
            self.notify(_("Sken zrušen – zobrazuji dosavadní výsledky."), severity="warning")
        else:
            n = sum(len(r.findings) for r in result.repos)
            self.notify(
                _("Sken dokončen: {len} repozitářů, {n} nálezů.").format(
                    len=len(result.repos), n=n
                ),
                title="repo-doctor",
            )
            failed = [r.name for r in result.repos if "fetch" in r.errors]
            if fetch and failed:
                self.notify(
                    _("Fetch selhal u {len} rep ({join}{v}) – důvod je v kartě repa.").format(
                        len=len(failed),
                        join=", ".join(failed[:3]),
                        v="…" if len(failed) > 3 else "",
                    ),
                    severity="warning",
                    timeout=10,
                )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        # `_scan` překresluje ještě za běhu workeru (`scanning` je tehdy True) – po jeho skutečném
        # skončení proto musí následovat poslední překreslení, jinak zůstane hlavička „skenuje“.
        if event.worker is self.scan_worker and event.state in TERMINAL_WORKER_STATES:
            self._refresh_views()

    def _on_scan_event(self, event: ScanEvent) -> None:
        if event.kind == "discovered":
            self.scan_total = event.total
        elif event.kind == "check":
            self.scan_current = f"{event.repo} · {event.check}"
        elif event.kind == "repo_done" and event.result is not None:
            self.scan_done = event.done
            self._upsert(event.result)
        self._refresh_views(throttle=event.kind == "check")

    def _upsert(self, repo: RepoResult) -> None:
        repos = [r for r in self.result.repos if r.path != repo.path]
        repos.append(repo)
        self.result.repos = repos

    def _refresh_views(self, *, throttle: bool = False) -> None:
        if self._dashboard is not None and self._dashboard.is_attached:
            self._dashboard.refresh_data(light=throttle)

    def exit(
        self,
        result: int | None = None,
        return_code: int = 0,
        message: RenderableType | None = None,
    ) -> None:
        self.stop_work()
        super().exit(result, return_code, message)

    def on_inbox_changed(self) -> None:
        """Notifikace nebo průběh workflow se změnily – přehled je překreslí."""
        self._refresh_views()

    def stop_work(self) -> None:
        """Zastaví veškerou práci na pozadí, aby proces po zavření nečekal na sken."""
        self.workflows.stop_all()
        if self.scanner is not None:
            self.scanner.cancel()
        gitwrap.abort_all()

    def cancel_scan(self) -> None:
        if not self.scanning or self.scanner is None:
            self.notify(_("Žádný sken neběží."))
            return
        self.scanner.cancel()

    # ------------------------------------------------------------------ akce
    def help_context(self) -> str:
        """Kontext nejvyšší obrazovky, která má vlastní nápovědu (dialogy se řídí podkladem)."""
        for screen in reversed(self.screen_stack):
            context = getattr(screen, "help_context", None)
            if isinstance(context, str):
                return context
        return "global"

    def action_help(self) -> None:
        from repo_doctor.tui.screens.help import HelpScreen

        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen(self.help_context()))

    def action_help_all(self) -> None:
        from repo_doctor.tui.screens.help import HelpScreen

        if isinstance(self.screen, HelpScreen):
            self.screen.show("all")
        else:
            self.push_screen(HelpScreen(self.help_context(), "all"))

    def action_scan_all(self) -> None:
        self.start_scan()

    def action_scan_fetch(self) -> None:
        self.start_scan(fetch=True)

    def action_scan_repo(self) -> None:
        if self.selected_path is None:
            self.notify(_("Není vybrané žádné repo."), severity="warning")
            return
        self.start_scan(self.selected_path)

    def action_cancel_scan(self) -> None:
        self.cancel_scan()

    def action_toggle_offline(self) -> None:
        self.offline = not self.offline
        self.result.offline = self.offline
        state = _("zapnutý – síťové kontroly budou přeskočeny") if self.offline else _("vypnutý")
        self.notify(
            _("Offline režim {state}. {key} spustí nový sken.").format(
                state=state, key=self.key("scan_all")
            )
        )
        self._refresh_views()

    def action_search(self) -> None:
        if self._in_wizard():
            return
        self._pop_to_dashboard()
        self.dashboard.open_search()

    def _in_wizard(self) -> bool:
        from repo_doctor.tui.screens.wizard import WizardScreen

        if isinstance(self.screen, WizardScreen):
            self.notify(
                _("Nejdřív dokonči průvodce (ctrl+s) nebo ho přeskoč (esc)."), severity="warning"
            )
            return True
        return False

    def action_go_back(self) -> None:
        from repo_doctor.tui.screens.dashboard import DashboardScreen

        if self._in_wizard():
            return

        if isinstance(self.screen, DashboardScreen):
            self.notify(_("Aplikaci ukončíš klávesou {key}.").format(key=self.key("quit")))
        elif len(self.screen_stack) > 1:
            self.pop_screen()

    def _pop_to_dashboard(self) -> None:
        from repo_doctor.tui.screens.dashboard import DashboardScreen

        while len(self.screen_stack) > 2 and not isinstance(self.screen, DashboardScreen):
            self.pop_screen()

    def go_home(self) -> bool:
        """Vrátí se na přehled (v průvodci ne). Vrací, zda se to povedlo."""
        if self._in_wizard():
            return False
        self._pop_to_dashboard()
        return True

    def action_open_screen(self, name: str) -> None:
        from repo_doctor.tui.screens import (
            cleanup,
            export,
            folders,
            forges,
            remote,
            settings,
            sync,
            tools,
        )

        factories: dict[str, Callable[[], Any]] = {
            "folders": folders.FoldersScreen,
            "forges": forges.ForgesScreen,
            "remote": remote.RemoteScreen,
            "settings": settings.SettingsScreen,
            "export": export.ExportScreen,
            "cleanup": cleanup.CleanupScreen,
            "sync": sync.SyncScreen,
            "tools": tools.ToolsScreen,
        }
        if self.go_home():
            self.push_screen(factories[name]())

    def open_detail(self, path: str) -> None:
        from repo_doctor.tui.screens.detail import DetailScreen

        self.selected_path = path
        if self.go_home():
            self.push_screen(DetailScreen(path))

    def open_publish(self, path: str) -> None:
        from repo_doctor.tui.screens.publish import PublishScreen

        self.selected_path = path
        self.push_screen(PublishScreen(path))

    def open_fix(self, path: str, focus_check: str | None = None) -> None:
        from repo_doctor.tui.screens.fix import FixScreen

        self.selected_path = path
        self.push_screen(FixScreen(path, focus_check))


def run(roots_override: list[str] | None) -> int:
    store = ConfigStore()
    try:
        config = store.load()
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 2
    try:
        build_keymap(config.keys)
    except KeymapError as err:
        print(err, file=sys.stderr)
        print(
            _("Oprav sekci [keys] v {config_file}").format(config_file=paths.config_file()),
            file=sys.stderr,
        )
        return 2
    app = RepoDoctorApp(store=store, roots_override=roots_override)
    code = app.run()
    return code or 0
