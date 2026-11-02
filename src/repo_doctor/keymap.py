"""Definice klávesových zkratek (jediný zdroj pravdy pro TUI, nápovědu i README).

Uživatel může akce přemapovat v `config.toml`, sekce `[keys]` (akce → klávesa).
Neplatné nebo kolidující mapování je chyba se srozumitelným popisem.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from repo_doctor.i18n import _

CONTEXTS: dict[str, str] = {
    "global": _("Globální"),
    "nav": _("Pohyb"),
    "dashboard": _("Přehled (triáž)"),
    "detail": _("Karta repa"),
    "publish": _("Odeslání oprav"),
    "fix": _("Léčba"),
    "folders": _("Složky"),
    "forges": _("Hostingy"),
    "remote": _("Vzdálená repa"),
    "settings": _("Nastavení"),
    "tools": _("Nástroje"),
    "export": _("Export"),
    "cleanup": _("Úklid"),
    "sync": _("Synchronizace"),
    "wizard": _("Průvodce prvním spuštěním"),
    "dialog": _("Formulářové dialogy"),
}


@dataclass(frozen=True)
class KeyAction:
    id: str
    key: str
    context: str
    description: str
    action: str
    show: bool = True
    label: str = ""  # jak klávesu zobrazit (výchozí: key)
    also: tuple[str, ...] = ()  # další kontexty, kde akce platí (jedna klávesa, jedno přemapování)

    @property
    def display(self) -> str:
        return self.label or display_key(self.key)

    @property
    def contexts(self) -> tuple[str, ...]:
        return (self.context, *self.also)


def _a(
    id: str,
    key: str,
    ctx: str,
    desc: str,
    action: str | None = None,
    *,
    show: bool = True,
    label: str = "",
    also: tuple[str, ...] = (),
) -> KeyAction:
    return KeyAction(id, key, ctx, desc, action or id, show, label, also)


ACTIONS: tuple[KeyAction, ...] = (
    # globální
    _a("help", "question_mark", "global", _("nápověda"), label="?"),
    _a("help_all", "H", "global", _("úplná nápověda (vše)"), show=False),
    _a("command_palette", "ctrl+p", "global", _("příkazová paleta"), show=False),
    # přehled, karta repa a léčba nemají číslo: ⏎ a f na přehledu, zpět q / esc (#32)
    _a("screen_folders", "1", "global", _("složky s repy"), "open_screen('folders')"),
    _a("screen_forges", "2", "global", _("hostingy (GitHub, Forgejo…)"), "open_screen('forges')"),
    _a("screen_remote", "3", "global", _("vzdálená repa"), "open_screen('remote')", show=False),
    _a("screen_settings", "4", "global", _("nastavení"), "open_screen('settings')", show=False),
    _a("screen_export", "5", "global", "export", "open_screen('export')", show=False),
    _a("screen_cleanup", "6", "global", _("úklid místa"), "open_screen('cleanup')", show=False),
    _a("screen_sync", "7", "global", _("synchronizace"), "open_screen('sync')", show=False),
    _a("screen_tools", "8", "global", _("nástroje"), "open_screen('tools')", show=False),
    _a("search", "slash", "global", _("hledat repo"), label="/"),
    _a("scan_all", "R", "global", _("skenovat všechna repa")),
    _a("scan_repo", "r", "global", _("skenovat vybrané repo")),
    _a("scan_fetch", "F", "global", _("skenovat vše + git fetch (novinky z remote)"), show=False),
    _a("cancel_scan", "x", "global", _("zrušit běžící sken"), show=False),
    _a("toggle_offline", "O", "global", _("offline režim (bez síťových kontrol)"), show=False),
    _a("toggle_theme", "t", "global", _("světlé / tmavé téma"), show=False),
    _a("back", "q", "global", _("zpět"), "go_back"),
    _a("quit", "ctrl+q", "global", _("konec (zastaví i běžící sken)")),
    # pohyb (platí ve všech seznamech)
    _a("cursor_down", "j", "nav", _("dolů"), show=False),
    _a("cursor_up", "k", "nav", _("nahoru"), show=False),
    _a("cursor_top", "g", "nav", _("začátek seznamu"), show=False),
    _a("cursor_bottom", "G", "nav", _("konec seznamu"), show=False),
    _a("half_page_down", "ctrl+d", "nav", _("půl stránky dolů"), show=False),
    _a("half_page_up", "ctrl+u", "nav", _("půl stránky nahoru"), show=False),
    _a("focus_next", "tab", "nav", _("další panel"), show=False),
    _a("focus_previous", "shift+tab", "nav", _("předchozí panel"), show=False),
    # přehled
    _a("open_card", "enter", "dashboard", _("karta repa"), label="⏎"),
    _a("open_fix", "f", "dashboard", _("léčba (opravy)"), also=("detail",)),
    _a("collapse_band", "z", "dashboard", _("sbalit / rozbalit skupinu")),
    _a("sort_cycle", "S", "dashboard", _("řadit podle sloupce"), show=False),
    _a("sort_reverse", "s", "dashboard", _("obrátit řazení"), show=False),
    _a("filter_high", "alt+1", "dashboard", _("filtr ▲ HIGH"), show=False),
    _a("filter_medium", "alt+2", "dashboard", _("filtr ◆ MED"), show=False),
    _a("filter_low", "alt+3", "dashboard", _("filtr ● LOW"), show=False),
    _a("problems_only", "p", "dashboard", _("jen problémová repa"), show=False),
    _a(
        "group_cycle",
        "alt+g",
        "dashboard",
        _("seskupit: triáž / složka / hosting / remote"),
        show=False,
    ),
    _a("toggle_sidebar", "b", "dashboard", _("skrýt / ukázat levý panel"), show=False),
    # karta repa
    _a("allowlist", "a", "detail", _("skrýt nález (allowlist)")),
    _a("open_editor", "o", "detail", _("otevřít v editoru")),
    _a("open_web", "w", "detail", _("otevřít na webu")),
    _a("copy_path", "y", "detail", _("kopírovat cestu")),
    _a("export_sbom", "b", "detail", _("export SBOM (CycloneDX)"), show=False),
    _a("publish_open", "p", "detail", _("odeslání oprav: push, PR, merge a stav CI")),
    # odeslání oprav (push, PR/MR, merge, stav CI)
    _a("publish_push", "P", "publish", _("poslat opravnou větev (push)")),
    _a("publish_pr", "C", "publish", _("vytvořit PR/MR z opravné větve")),
    _a("publish_merge", "M", "publish", _("mergnout PR na hostingu (po zelené CI)")),
    _a("publish_merge_local", "L", "publish", _("lokální merge opravy (fast-forward)")),
    _a("publish_push_default", "U", "publish", _("poslat výchozí větev (po lokálním merge)")),
    _a("publish_refresh", "ctrl+r", "publish", _("načíst stav znovu (PR, CI)")),
    _a("publish_open_web", "w", "publish", _("otevřít PR/MR na webu")),
    # léčba
    _a("toggle_fix", "space", "fix", _("vybrat opravu"), label="space"),
    _a("select_all", "A", "fix", _("vybrat vše")),
    _a("confirm_fix", "enter", "fix", _("provést vybrané"), label="⏎"),
    # úklid
    _a("cleanup_toggle", "space", "cleanup", _("vybrat repo"), label="space"),
    _a("cleanup_all", "A", "cleanup", _("vybrat vše / nic")),
    _a("cleanup_apply", "enter", "cleanup", _("přesunout do koše"), label="⏎"),
    _a("cleanup_permanent", "D", "cleanup", _("trvale smazat"), show=False),
    # synchronizace
    _a("sync_fetch", "f", "sync", _("stáhnout novinky (fetch)")),
    _a("sync_toggle", "space", "sync", _("vybrat repo"), label="space"),
    _a("sync_apply", "enter", "sync", _("dohnat vybrané (fast-forward)"), label="⏎"),
    # složky
    _a("folder_add", "n", "folders", _("přidat složku")),
    _a("folder_edit", "e", "folders", _("upravit složku")),
    _a("folder_delete", "d", "folders", _("odebrat složku")),
    _a("folder_toggle", "space", "folders", _("zapnout / vypnout"), label="space"),
    # hostingy
    _a("forge_add", "n", "forges", _("přidat hosting")),
    _a("forge_edit", "e", "forges", _("upravit hosting")),
    _a("forge_delete", "d", "forges", _("odebrat hosting")),
    _a("forge_test", "T", "forges", _("otestovat připojení")),
    # vzdálená repa
    _a("clone", "c", "remote", _("naklonovat repo")),
    _a("remote_view", "v", "remote", _("přepnout: chybí lokálně ↔ jen lokálně")),
    _a("remote_refresh", "ctrl+r", "remote", _("načíst znovu"), show=False),
    # nastavení
    _a("settings_allow_remove", "d", "settings", _("odebrat z allowlistu"), show=False),
    # nástroje
    _a("tools_install", "i", "tools", _("nainstalovat vybraný")),
    _a("tools_install_all", "I", "tools", _("nainstalovat vše")),
    _a("tools_update", "u", "tools", _("aktualizovat stažené")),
    _a("tools_remove", "d", "tools", _("odebrat stažený")),
    # export
    _a("export", "e", "export", _("exportovat report")),
    # průvodce prvním spuštěním
    _a("wizard_finish", "ctrl+s", "wizard", _("uložit a skenovat"), "finish"),
    _a("wizard_add_forge", "ctrl+n", "wizard", _("přidat hosting"), "add_forge"),
    # formulářové dialogy (složka, hosting, klon)
    _a("dialog_save", "ctrl+s", "dialog", _("uložit / potvrdit formulář"), "save"),
)

BY_ID: dict[str, KeyAction] = {a.id: a for a in ACTIONS}

# Sloučené a odebrané akce (#32) – staré názvy v [keys] dál fungují nebo se tiše ignorují.
LEGACY_IDS = {
    "dash_fix": "open_fix",
    "detail_fix": "open_fix",
    "screen_fix": "open_fix",
    "screen_detail": "open_card",
}
REMOVED_IDS = {"screen_overview"}  # na přehled vede q / esc

_SYMBOLS = {
    "?": "question_mark",
    "/": "slash",
    " ": "space",
    "⏎": "enter",
    "return": "enter",
    "esc": "escape",
}
_NAMED = {
    "enter",
    "escape",
    "tab",
    "space",
    "backspace",
    "delete",
    "insert",
    "home",
    "end",
    "pageup",
    "pagedown",
    "up",
    "down",
    "left",
    "right",
    "question_mark",
    "slash",
    "exclamation_mark",
    "at",
    "number_sign",
    "dollar_sign",
    "percent_sign",
    "circumflex_accent",
    "ampersand",
    "asterisk",
    "plus",
    "minus",
    "equals_sign",
    "comma",
    "full_stop",
    "colon",
    "semicolon",
    "less_than_sign",
    "greater_than_sign",
    "underscore",
    "left_square_bracket",
    "right_square_bracket",
    "left_curly_bracket",
    "right_curly_bracket",
    "vertical_line",
    "tilde",
    "grave_accent",
    "apostrophe",
    "quotation_mark",
    "backslash",
} | {f"f{i}" for i in range(1, 25)}
_KEY_RE = re.compile(r"^((ctrl|alt|shift|super|meta)\+)*([A-Za-z0-9]|[a-z_0-9]+)$")
RESERVED = {"escape"}  # Esc vždy znamená zpět/zrušit


class KeymapError(Exception):
    """Neplatné mapování kláves v [keys]."""


def normalize_key(key: str) -> str:
    key = key.strip()
    key = _SYMBOLS.get(key, key)
    parts = key.split("+")
    base = parts[-1]
    base = _SYMBOLS.get(base, base)
    mods = [m.lower() for m in parts[:-1]]
    if len(base) > 1:
        base = base.lower()
    return "+".join([*sorted(mods), base])


def display_key(key: str) -> str:
    rev = {"question_mark": "?", "slash": "/", "enter": "⏎", "escape": "esc", "space": "space"}
    parts = key.split("+")
    parts[-1] = rev.get(parts[-1], parts[-1])
    return "+".join(parts)


def is_valid_key(key: str) -> bool:
    norm = normalize_key(key)
    m = _KEY_RE.match(norm)
    if not m:
        return False
    base = m.group(3)
    return len(base) == 1 or base in _NAMED


def _conflicts(ctx_a: str, ctx_b: str) -> bool:
    if ctx_a == ctx_b:
        return True
    return "global" in (ctx_a, ctx_b) or "nav" in (ctx_a, ctx_b)


def migrate_overrides(overrides: Mapping[str, str]) -> dict[str, str]:
    """Převede staré názvy akcí z [keys] na současné (nový název má přednost)."""
    out: dict[str, str] = {}
    for action_id, key in overrides.items():
        if action_id in REMOVED_IDS:
            continue
        new = LEGACY_IDS.get(action_id, action_id)
        if new != action_id and new in overrides:
            continue
        out.setdefault(new, key)
    return out


def build_keymap(overrides: Mapping[str, str]) -> dict[str, str]:
    """Vrátí {id akce: klávesa} po aplikaci přemapování. Při chybě KeymapError."""
    problems: list[str] = []
    result = {a.id: a.key for a in ACTIONS}
    for action_id, key in migrate_overrides(overrides).items():
        if action_id not in BY_ID:
            problems.append(
                _("neznámá akce `{action_id}` (platné: {join})").format(
                    action_id=action_id, join=", ".join(sorted(BY_ID))
                )
            )
            continue
        if not isinstance(key, str) or not is_valid_key(key):
            problems.append(
                _("`{action_id}`: neplatná klávesa {key!r}").format(action_id=action_id, key=key)
            )
            continue
        if normalize_key(key) in RESERVED:
            problems.append(
                _("`{action_id}`: klávesa {key!r} je vyhrazená (Esc = zpět)").format(
                    action_id=action_id, key=key
                )
            )
            continue
        result[action_id] = normalize_key(key)
    seen: dict[str, list[KeyAction]] = {}
    for a in ACTIONS:
        seen.setdefault(normalize_key(result[a.id]), []).append(a)
    for key, actions in sorted(seen.items()):
        for i, first in enumerate(actions):
            for second in actions[i + 1 :]:
                if any(_conflicts(a, b) for a in first.contexts for b in second.contexts):
                    problems.append(
                        _(
                            "kolize: klávesa {display_key!r} je přiřazena akcím `{id}` ({v}) a `{id2}` ({v2})"
                        ).format(
                            display_key=display_key(key),
                            id=first.id,
                            v=CONTEXTS[first.context],
                            id2=second.id,
                            v2=CONTEXTS[second.context],
                        )
                    )
    if problems:
        raise KeymapError(
            _("Neplatné mapování kláves v [keys]:\n") + "\n".join(f"  • {p}" for p in problems)
        )
    return result


def key_label(action_id: str, keymap: Mapping[str, str] | None = None) -> str:
    """Jak klávesu akce zobrazit (respektuje přemapování)."""
    action = BY_ID[action_id]
    key = (keymap or {}).get(action_id, action.key)
    return action.label if action.label and key == action.key else display_key(key)


def grouped(
    keymap: Mapping[str, str] | None = None, contexts: tuple[str, ...] | None = None
) -> dict[str, list[tuple[str, str]]]:
    """Pro nápovědu: {kontext: [(klávesa, popis)]} podle aktuálního mapování.

    `contexts` omezí a seřadí výpis (např. kontext obrazovky, pohyb, globální).
    """
    keymap = keymap or {a.id: a.key for a in ACTIONS}
    wanted = contexts if contexts is not None else tuple(CONTEXTS)
    out: dict[str, list[tuple[str, str]]] = {CONTEXTS[c]: [] for c in wanted}
    for a in ACTIONS:
        key = keymap.get(a.id, a.key)
        shown = a.label if a.label and key == a.key else display_key(key)
        for ctx in a.contexts:
            if ctx in wanted:
                out[CONTEXTS[ctx]].append((shown, a.description))
    return {k: v for k, v in out.items() if v}
