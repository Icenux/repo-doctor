"""Hostingy: připojené účty GitHub, Gitea/Forgejo a GitLab. Token se nikdy nezobrazí."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from repo_doctor import netpolicy
from repo_doctor.config import ConfigError, ForgeConfig
from repo_doctor.forges import HTTP_NOTE, build_forge, insecure_reason
from repo_doctor.forges.base import ConnectionReport
from repo_doctor.forges.tokens import (
    TokenError,
    delete_from_keyring,
    resolve_token,
    store_in_keyring,
)
from repo_doctor.i18n import _
from repo_doctor.masking import redact
from repo_doctor.sshhelp import forge_ssh_warning
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.screens.base import BaseScreen
from repo_doctor.tui.screens.dialogs import ConfirmDialog
from repo_doctor.tui.screens.forms import ForgeDialog, ForgeFormResult
from repo_doctor.tui.widgets.keybar import KeyBar

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp

SOURCE_LABELS = {
    "keyring": _("klíčenka"),
    "env": _("proměnná"),
    "cmd": _("příkaz"),
    "none": _("bez tokenu"),
}


async def test_forge(
    config: ForgeConfig,
    transport: httpx.AsyncBaseTransport | None = None,
    settings_key: str | None = None,
) -> ConnectionReport:
    """Otestuje připojení (token se řeší ve vlákně, protože může spouštět příkaz)."""
    import asyncio

    if reason := insecure_reason(config, settings_key):
        return ConnectionReport(False, error=reason)
    try:
        token = await asyncio.to_thread(resolve_token, config)
    except TokenError as err:
        return ConnectionReport(False, error=f"token: {err}")
    try:
        forge = build_forge(
            config, token=token.reveal() if token else None, ttl=0, transport=transport
        )
    except OSError as err:
        return ConnectionReport(False, error=f"TLS: {err}")
    try:
        report = await forge.test_connection()
        if report.ok and (ssh := await forge_ssh_warning(forge, config)):
            report.warnings.append(ssh)
        return report
    finally:
        await forge.client.aclose()


class ForgesScreen(BaseScreen):
    help_context = "forges"
    BINDINGS = [*BaseScreen.BINDINGS, *bindings("forges")]

    def __init__(self) -> None:
        super().__init__()
        self.reports: dict[str, ConnectionReport | None] = {}

    @property
    def rd(self) -> RepoDoctorApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(classes="page panel") as page:
            page.border_title = " hostingy "
            yield Static(id="forges-note", classes="page-header")
            yield DataTable(id="forges", cursor_type="row")
            yield Static(id="forge-detail", classes="page-header")
        yield KeyBar(
            ["forge_add", "forge_edit", "forge_delete", "forge_test", ("esc", _("zpět")), "help"]
        )

    def on_mount(self) -> None:
        self.refresh_table()
        self.query_one(DataTable).focus()

    def on_config_changed(self) -> None:
        self.refresh_table()

    def refresh_table(self) -> None:
        p = self.rd.palette
        note = Text(
            _(
                "Připojení je volitelné. Sken z hostingu jen čte, zapisuje se jen PR/MR a merge z Odeslání oprav po potvrzení."
            ),
            style=Style(color=p.muted),
        )
        self.query_one("#forges-note", Static).update(note)
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear(columns=True)
        table.add_columns(
            _("název"), _("připojení"), _("typ"), "TLS", "token", "URL", _("uživatel / org")
        )
        for i, f in enumerate(self.rd.config.forges):
            tls = Text(_("ověřeno"), style=Style(color=p.ok))
            if f.ca_bundle:
                tls = Text(_("vlastní CA"), style=Style(color=p.ok))
            elif not f.verify_tls:
                tls = Text(_("⚠ vypnuto"), style=Style(color=p.high))
            if netpolicy.is_http(f.base_url):
                tls = (
                    Text(_("⚠ http (povoleno)"), style=Style(color=p.med))
                    if insecure_reason(f) is None
                    else Text(_("✗ http"), style=Style(color=p.high))
                )
            rep = self.reports.get(f.name, None)
            if f.name in self.reports and rep is None:
                status = Text(_("testuji…"), style=Style(color=p.muted))
            elif rep is None:
                status = Text(_("neotestováno (T)"), style=Style(color=p.dim))
            elif rep.ok:
                status = Text(
                    _("✓ {v} · {repo_count} rep").format(
                        v=rep.user or "", repo_count=rep.repo_count
                    ),
                    style=Style(color=p.ok),
                )
            else:
                status = Text(_("✗ chyba"), style=Style(color=p.high))
            table.add_row(
                f.name,
                status,
                f.type,
                tls,
                SOURCE_LABELS.get(f.token_source or "none", "?")
                + (f" ({f.token_env})" if f.token_env else ""),
                f.base_url,
                " / ".join(x for x in (f.user, f.org) if x) or "—",
                key=str(i),
            )
        if not self.rd.config.forges:
            table.add_row(
                "—",
                Text(_("žádný hosting – n přidá (volitelné)"), style=Style(color=p.muted)),
                "",
                "",
                "",
                "",
                "",
            )
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))
        self._detail()

    def _detail(self) -> None:
        p = self.rd.palette
        idx = self._index()
        text = Text()
        if idx is not None:
            f = self.rd.config.forges[idx]
            if netpolicy.is_http(f.base_url):
                reason = insecure_reason(f, self.rd.key("screen_settings"))
                text.append(
                    f"! {_(HTTP_NOTE)}\n" if reason is None else f"✗ {reason}\n",
                    style=Style(color=p.med if reason is None else p.high),
                )
            rep = self.reports.get(f.name)
            if rep is not None:
                if rep.error:
                    text.append(f"✗ {redact(rep.error)}\n", style=Style(color=p.high))
                if rep.scopes:
                    text.append(f"scopes: {', '.join(rep.scopes)}\n", style=Style(color=p.muted))
                for w in rep.warnings:
                    text.append(f"! {w}\n", style=Style(color=p.med))
        self.query_one("#forge-detail", Static).update(text)

    def on_data_table_row_highlighted(self) -> None:
        self._detail()

    def _index(self) -> int | None:
        table = self.query_one(DataTable)
        if not self.rd.config.forges or table.row_count == 0:
            return None
        return min(table.cursor_row, len(self.rd.config.forges) - 1)

    def _saved(self, result: ForgeFormResult, index: int | None) -> None:
        try:
            if result.allow_http_host:  # potvrzeno v dialogu (#68), stejně jako v průvodci
                current = list(self.rd.store.config.allow_http_hosts)
                if result.allow_http_host not in current:
                    self.rd.store.set_top("allow_http_hosts", [*current, result.allow_http_host])
            config = self.rd.store.upsert_forge(result.config, index)
        except ConfigError as err:
            self.notify(str(err), severity="error", timeout=10)
            return
        if result.token:
            try:
                store_in_keyring(result.config.name, result.token)
            except TokenError as err:
                self.notify(
                    _("{err}. Použij raději token_env nebo token_cmd.").format(err=err),
                    severity="error",
                    timeout=10,
                )
        self.reports.pop(result.config.name, None)
        self.rd.config_changed(config)
        self.notify(
            _("Hosting {name} uložen. T otestuje připojení.").format(name=result.config.name)
        )

    def action_forge_add(self) -> None:
        names = {f.name for f in self.rd.config.forges}

        def done(result: ForgeFormResult | None) -> None:
            if result is not None:
                self._saved(result, None)

        self.app.push_screen(ForgeDialog(None, names, offer_http_allow=True), done)

    def action_forge_edit(self) -> None:
        idx = self._index()
        if idx is None:
            return
        names = {f.name for f in self.rd.config.forges}

        def done(result: ForgeFormResult | None) -> None:
            if result is not None:
                self._saved(result, idx)

        self.app.push_screen(
            ForgeDialog(self.rd.config.forges[idx], names, offer_http_allow=True), done
        )

    def action_forge_delete(self) -> None:
        idx = self._index()
        if idx is None:
            return
        forge = self.rd.config.forges[idx]

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                config = self.rd.store.remove_forge(idx)
            except ConfigError as err:
                self.notify(str(err), severity="error")
                return
            if forge.token_source == "keyring":
                delete_from_keyring(forge.name)
            self.rd.config_changed(config)
            self.notify(_("Hosting {name} odebrán.").format(name=forge.name))

        extra = (
            _("\nToken v systémové klíčence se také smaže.")
            if forge.token_source == "keyring"
            else ""
        )
        self.app.push_screen(
            ConfirmDialog(
                _("Odebrat hosting"),
                _(
                    "\nOdebrat hosting {name} ({type})?\nNa hostingu se nic nezmění.{extra}\n"
                ).format(name=forge.name, type=forge.type, extra=extra),
                confirm_label=_("Odebrat"),
                danger=True,
            ),
            done,
        )

    def action_forge_test(self) -> None:
        idx = self._index()
        if idx is None:
            return
        forge = self.rd.config.forges[idx]
        self.reports[forge.name] = None
        self.refresh_table()
        self.run_test(forge)

    @work(exclusive=False, group="forge-test")
    async def run_test(self, forge: ForgeConfig) -> None:
        report = await test_forge(forge, self.rd.forge_transport, self.rd.key("screen_settings"))
        self.reports[forge.name] = report
        self.refresh_table()
        if report.ok:
            self.notify(
                _("{name}: připojeno jako {user}, vidí {repo_count} repozitářů.").format(
                    name=forge.name, user=report.user, repo_count=report.repo_count
                )
            )
            for w in report.warnings:
                if w.startswith("SSH: "):
                    self.notify(f"{forge.name}: {w}", severity="warning", timeout=15)
        else:
            self.notify(
                f"{forge.name}: {redact(report.error or _('chyba'))}", severity="error", timeout=10
            )
