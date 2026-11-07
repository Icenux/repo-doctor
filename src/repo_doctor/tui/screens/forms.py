"""Dialogy pro složky, hostingy a klonování."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from repo_doctor.config import ForgeConfig, RootConfig, format_validation_error
from repo_doctor.discovery import suggest_depth
from repo_doctor.forges.base import ForgeRepo
from repo_doctor.i18n import _
from repo_doctor.paths import expand_path
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.screens.dialogs import ConfirmDialog
from repo_doctor.tui.widgets.forms import (
    ExistingDir,
    IntRange,
    PathInput,
    PathSuggester,
    split_list,
)


class RootDialog(ModalScreen[RootConfig | None]):
    """Přidání / úprava sledované složky."""

    help_context = "dialog"

    BINDINGS = [Binding("escape", "cancel", _("zrušit")), *bindings("dialog")]

    def __init__(self, root: RootConfig | None = None) -> None:
        super().__init__()
        self.root = root
        self._suggested: str | None = None  # hloubka, kterou do pole dosadil návrh (ne uživatel)

    def compose(self) -> ComposeResult:
        r = self.root
        with VerticalScroll(classes="dialog wide") as box:
            box.border_title = _(" Upravit složku ") if r else _(" Přidat složku ")
            yield Label(_("Cesta (tab doplní návrh)"))
            yield PathInput(
                compact=True,
                value=r.path if r else "",
                placeholder="~/projekty",
                suggester=PathSuggester(),
                validators=[ExistingDir()],
                id="path",
            )
            yield Static("", classes="hint", id="found")
            yield Label(_("Hloubka prohledávání (0–12)"))
            yield Input(
                compact=True,
                value=str(r.depth if r else 3),
                validators=[IntRange(0, 12)],
                id="depth",
            )
            yield Label(_("Vylučující vzory (čárkami, např. **/archiv/**)"))
            yield Input(compact=True, value=", ".join(r.exclude) if r else "", id="exclude")
            yield Checkbox(
                compact=True,
                label=_("Následovat symlinky"),
                value=r.follow_symlinks if r else False,
                id="symlinks",
            )
            yield Checkbox(
                compact=True, label=_("Zapnuto"), value=r.enabled if r else True, id="enabled"
            )
            yield Static("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button(_("Zrušit"), id="cancel")
                yield Button(_("Uložit"), id="ok", variant="primary")
            yield Static(dialog_hint(self), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()
        if self.root:
            self._probe(self.root.path)

    @on(Input.Changed, "#path")
    def _path_changed(self, event: Input.Changed) -> None:
        if event.validation_result and event.validation_result.is_valid:
            self._probe(event.value)
        else:
            self.query_one("#found", Static).update("")

    @work(thread=True, exclusive=True, group="probe")
    def _probe(self, value: str) -> None:
        depth, count = suggest_depth(expand_path(value), max_depth=5)
        text = (
            _("nalezeno {count} repozitářů · navržená hloubka {depth}").format(
                count=count, depth=depth
            )
            if count
            else _("zatím žádné git repozitáře (do hloubky 5)")
        )
        self.app.call_from_thread(self._probed, value, text, depth, count)

    def _probed(self, value: str, text: str, depth: int, count: int) -> None:
        # vlákno jde zrušit až po doběhnutí: pozdní výsledek pro rozepsanou cestu nesmí přepsat aktuální
        if value != self.query_one("#path", Input).value:
            return
        self.query_one("#found", Static).update(text)
        depth_in = self.query_one("#depth", Input)
        # u nové složky pole předvyplníme návrhem, ale uživatelovu hodnotu nepřepisujeme
        if self.root is None and count and depth_in.value in ("3", self._suggested):
            self._suggested = str(depth)
            depth_in.value = self._suggested

    def _build(self) -> RootConfig | None:
        error = self.query_one("#error", Static)
        path_in = self.query_one("#path", Input)
        depth_in = self.query_one("#depth", Input)
        for inp in (path_in, depth_in):
            res = inp.validate(inp.value)
            if res and not res.is_valid:
                label = _("Cesta") if inp.id == "path" else _("Hloubka")
                error.update(f"{label}: {'; '.join(res.failure_descriptions)}")
                inp.focus()
                return None
        try:
            return RootConfig(
                path=path_in.value.strip().rstrip("/") or "/",
                depth=int(depth_in.value),
                exclude=split_list(self.query_one("#exclude", Input).value),
                follow_symlinks=self.query_one("#symlinks", Checkbox).value,
                enabled=self.query_one("#enabled", Checkbox).value,
            )
        except ValidationError as err:
            error.update(format_validation_error(err, _("složka")))
            return None

    def action_save(self) -> None:
        root = self._build()
        if root is not None:
            self.dismiss(root)

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def dialog_hint(screen: ModalScreen[Any]) -> str:
    """Jednotná nápověda formulářových dialogů (klávesa uložení podle aktuálního mapování)."""
    key = getattr(screen.app, "key", lambda _id: "ctrl+s")("dialog_save")
    return _("tab další pole · enter / {key} uložit · esc zrušit").format(key=key)


@dataclass
class ForgeFormResult:
    config: ForgeConfig
    token: str | None  # nový token pro klíčenku (jinak None)
    allow_http_host: str | None = None  # host, který uživatel potvrdil pro allow_http_hosts


TOKEN_SOURCES = [
    (_("systémová klíčenka (doporučeno)"), "keyring"),
    (_("proměnná prostředí"), "env"),
    (_("příkaz (např. pass)"), "cmd"),
    (_("bez tokenu (jen veřejná data)"), "none"),
]
FORGE_TYPES = [
    ("GitHub", "github"),
    ("Forgejo", "forgejo"),
    ("Gitea", "gitea"),
    ("GitLab", "gitlab"),
]


class ForgeDialog(ModalScreen[ForgeFormResult | None]):
    """Přidání / úprava hostingu. Token se nikdy nezobrazí (pole je maskované a prázdné)."""

    help_context = "dialog"

    BINDINGS = [Binding("escape", "cancel", _("zrušit")), *bindings("dialog")]

    def __init__(
        self,
        forge: ForgeConfig | None = None,
        existing_names: set[str] | None = None,
        *,
        offer_http_allow: bool = False,
    ) -> None:
        super().__init__()
        self.forge = forge
        self.existing = (existing_names or set()) - ({forge.name} if forge else set())
        # u http:// URL mimo allow_http_hosts nabídne přidání hosta (jinak jen chyba s návodem)
        self.offer_http_allow = offer_http_allow

    def compose(self) -> ComposeResult:
        f = self.forge
        with VerticalScroll(classes="dialog wide") as box:
            box.border_title = _(" Upravit hosting ") if f else _(" Přidat hosting ")
            yield Label(_("Typ"))
            yield Select(
                compact=True,
                options=FORGE_TYPES,
                value=f.type if f else "github",
                allow_blank=False,
                id="type",
            )
            yield Label(_("Název (jedinečný, např. github nebo domaci-forgejo)"))
            yield Input(compact=True, value=f.name if f else "", placeholder="github", id="name")
            yield Label(_("URL instance (prázdné = github.com / gitlab.com)"))
            yield Input(
                compact=True,
                value=f.url or "" if f else "",
                placeholder="https://git.tailnet.ts.net:3000",
                id="url",
            )
            yield Label(_("Uživatel / organizace (volitelné)"))
            with Horizontal(classes="row"):
                yield Input(
                    compact=True,
                    value=f.user or "" if f else "",
                    placeholder=_("uživatel"),
                    id="user",
                )
                yield Input(
                    compact=True,
                    value=f.org or "" if f else "",
                    placeholder=_("organizace / skupina"),
                    id="org",
                )
            yield Label(_("Zdroj tokenu"))
            yield Select(
                compact=True,
                options=TOKEN_SOURCES,
                value=(f.token_source if f else "keyring") or "keyring",
                allow_blank=False,
                id="source",
            )
            yield Input(
                compact=True,
                password=True,
                placeholder=_("token – uloží se do klíčenky (prázdné = beze změny)"),
                id="token",
            )
            yield Input(
                compact=True,
                value=f.token_env or "" if f else "",
                placeholder=_("název proměnné, např. FORGEJO_TOKEN"),
                id="token_env",
            )
            yield Input(
                compact=True,
                value=f.token_cmd or "" if f else "",
                placeholder=_("příkaz, např. pass show git/forgejo"),
                id="token_cmd",
            )
            yield Checkbox(
                compact=True,
                label=_("Ověřovat TLS certifikát"),
                value=f.verify_tls if f else True,
                id="verify",
            )
            yield Input(
                compact=True,
                value=f.ca_bundle or "" if f else "",
                placeholder=_("vlastní CA (cesta k .pem) – pro self-signed"),
                id="ca",
            )
            yield Static("", classes="error", id="tls-warning")
            yield Label(_("Protokol pro klonování"))
            yield Select(
                compact=True,
                options=[("SSH", "ssh"), ("HTTPS", "https")],
                value=f.clone_protocol if f else "ssh",
                allow_blank=False,
                id="proto",
            )
            yield Static("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button(_("Zrušit"), id="cancel")
                yield Button(_("Uložit"), id="ok", variant="primary")
            yield Static(
                _("Token se nikdy nezobrazí ani neuloží do config.toml.\n") + dialog_hint(self),
                classes="hint",
            )

    def on_mount(self) -> None:
        self._sync_fields()
        self.query_one("#type", Select).focus()

    @on(Select.Changed)
    @on(Checkbox.Changed)
    def _changed(self) -> None:
        self._sync_fields()

    def _sync_fields(self) -> None:
        source = self.query_one("#source", Select).value
        self.query_one("#token").display = source == "keyring"
        self.query_one("#token_env").display = source == "env"
        self.query_one("#token_cmd").display = source == "cmd"
        verify = self.query_one("#verify", Checkbox).value
        self.query_one("#tls-warning", Static).update(
            ""
            if verify
            else _(
                "⚠ Ověřování TLS je vypnuté – spojení jde odposlechnout. Raději nastav vlastní CA."
            )
        )
        name = self.query_one("#name", Input)
        if not name.value and self.forge is None:
            name.placeholder = str(self.query_one("#type", Select).value)

    def _build(self) -> ForgeFormResult | None:
        def val(wid: str) -> str | None:
            v = self.query_one(f"#{wid}", Input).value.strip()
            return v or None

        ftype = str(self.query_one("#type", Select).value)
        name = val("name") or ftype
        source = str(self.query_one("#source", Select).value)
        error = self.query_one("#error", Static)
        if name in self.existing:
            error.update(_("Hosting s názvem {name} už existuje.").format(name=name))
            return None
        try:
            config = ForgeConfig(
                name=name,
                type=ftype,
                url=val("url"),
                user=val("user"),
                org=val("org"),
                token_source=source,
                token_env=val("token_env") if source == "env" else None,
                token_cmd=val("token_cmd") if source == "cmd" else None,
                verify_tls=self.query_one("#verify", Checkbox).value,
                ca_bundle=val("ca"),
                clone_protocol=str(self.query_one("#proto", Select).value),
                enabled=self.forge.enabled if self.forge else True,
            )
        except ValidationError as err:
            error.update(format_validation_error(err, "hosting"))
            return None
        token = self.query_one("#token", Input).value.strip() if source == "keyring" else ""
        return ForgeFormResult(config, token or None)

    def action_save(self) -> None:
        from repo_doctor.forges import insecure_reason

        result = self._build()
        if result is None:
            return
        reason = insecure_reason(
            result.config, getattr(self.app, "key", lambda _id: None)("screen_settings")
        )
        if reason is None:
            self.dismiss(result)
            return
        if not self.offer_http_allow:
            self.query_one("#error", Static).update(reason)
            return
        host = urlsplit(result.config.base_url).hostname or ""

        def answered(ok: bool | None) -> None:
            if ok:
                result.allow_http_host = host
                self.dismiss(result)
            else:
                self.query_one("#error", Static).update(reason)

        self.app.push_screen(
            ConfirmDialog(
                _("Nešifrované spojení"),
                _(
                    "URL instance používá http:// – token i data půjdou po síti bez šifrování.\nTo je v pořádku jen v důvěryhodné síti, kde šifrování zajišťuje VPN (např. Tailscale). Na veřejném internetu ho může kdokoli po cestě přečíst.\n\nPřidat {host} do allow_http_hosts?"
                ).format(host=host),
                confirm_label=_("Povolit http pro tento host"),
                danger=True,
            ),
            answered,
        )

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class CloneRequest:
    url: str
    dest: Path


class CloneDialog(ModalScreen[CloneRequest | None]):
    """Volba cílové složky pro klon. Výchozí tlačítko = Zrušit, existující cíl se odmítne."""

    help_context = "dialog"

    BINDINGS = [Binding("escape", "cancel", _("zrušit")), *bindings("dialog")]

    def __init__(self, repo: ForgeRepo, roots: list[RootConfig], protocol: str) -> None:
        super().__init__()
        self.repo = repo
        self.roots = [r for r in roots if r.enabled]
        self.protocol = protocol

    @property
    def url(self) -> str:
        return (
            (self.repo.clone_ssh if self.protocol == "ssh" else self.repo.clone_https)
            or self.repo.clone_https
            or self.repo.clone_ssh
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide") as box:
            box.border_title = _(" Naklonovat {full_name} ").format(full_name=self.repo.full_name)
            yield Label(_("Cílová sledovaná složka"))
            yield Select(
                compact=True,
                options=[(r.path, r.path) for r in self.roots],
                value=self.roots[0].path if self.roots else Select.BLANK,
                id="root",
            )
            yield Label(_("Název složky"))
            yield Input(compact=True, value=self.repo.name, id="dirname")
            yield Static("", id="target", classes="hint")
            yield Static("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button(_("Zrušit"), id="cancel")
                yield Button(_("Naklonovat"), id="ok", variant="primary")
            yield Static(
                _("Klonuje se jen po potvrzení; existující složku nic nepřepíše.\n")
                + dialog_hint(self),
                classes="hint",
            )

    def on_mount(self) -> None:
        self._update()
        self.query_one("#cancel", Button).focus()

    def _dest(self) -> Path | None:
        root = self.query_one("#root", Select).value
        name = self.query_one("#dirname", Input).value.strip()
        if not isinstance(root, str) or not name or "/" in name or name in (".", ".."):
            return None
        return expand_path(root) / name

    @on(Select.Changed)
    @on(Input.Changed)
    def _update(self) -> None:
        dest = self._dest()
        self.query_one("#target", Static).update(
            f"git clone {self.url}\n  → {dest}" if dest else ""
        )
        error = ""
        if dest is None:
            error = _("Vyber složku a platný název.")
        elif dest.exists():
            error = _("Cíl už existuje – klon ho nepřepíše, zvol jiný název.")
        self.query_one("#error", Static).update(error)

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def action_save(self) -> None:
        dest = self._dest()
        if dest is None or dest.exists() or not self.url:
            self._update()
            return
        self.dismiss(CloneRequest(self.url, dest))

    @on(Button.Pressed, "#cancel")
    def _cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
