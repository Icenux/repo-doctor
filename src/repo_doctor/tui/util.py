"""Drobné pomocné funkce pro TUI (formátování, systémové akce)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from rich.style import Style
from rich.text import Text

from repo_doctor.i18n import _
from repo_doctor.models import RepoResult


def relative_time(when: datetime | None, now: datetime) -> str:
    if when is None:
        return _("bez commitu")
    seconds = (now - when).total_seconds()
    if seconds < 3600:
        return _("před chvílí")
    hours = int(seconds // 3600)
    if hours < 24:
        return _("před {hours} h").format(hours=hours)
    days = int(seconds // 86400)
    if days == 1:
        return _("včera")
    if days < 60:
        return _("před {days} dny").format(days=days)
    months = days // 30
    if months < 24:
        return _("před {months} měs.").format(months=months)
    return _("před {v} lety").format(v=days // 365)


def git_state(repo: RepoResult) -> str:
    if repo.untrusted_owner:
        return "safe.dir ✗"
    parts = []
    if repo.state.uncommitted:
        parts.append(f"~{repo.state.uncommitted}")
    if repo.state.unpushed:
        parts.append(f"↑{repo.state.unpushed}")
    if repo.state.detached:
        parts.append("detached")
    if not repo.remotes:
        parts.append(_("bez remote"))
    if not parts and repo.archived:
        parts.append(_("archiv"))
    return " ".join(parts) or _("čisté")


def home_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


def printable(text: str) -> str:
    """Text z hostingu bez řídicích znaků (C0, C1, ESC…), aby nešlo posílat sekvence do terminálu."""
    return "".join(c if c.isprintable() else "?" for c in text)


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def pad(text: Text | str, width: int, style: Style | str | None = None) -> Text:
    t = text.copy() if isinstance(text, Text) else Text(text, style=style or "")
    t.truncate(width, overflow="ellipsis")
    t.pad_right(width - t.cell_len)
    return t


# Editory, které Nastavení nabídne, pokud jsou v PATH: (příkaz, název, grafický).
# Grafický se spouští na pozadí, terminálový pozastaví TUI, dokud ho nezavřeš.
KNOWN_EDITORS: tuple[tuple[str, str, bool], ...] = (
    ("nvim", "Neovim", False),
    ("vim", "Vim", False),
    ("vi", "vi", False),
    ("nano", "nano", False),
    ("micro", "micro", False),
    ("hx", "Helix", False),
    ("helix", "Helix", False),
    ("kak", "Kakoune", False),
    ("emacs", "Emacs", False),
    ("code", "VS Code", True),
    ("codium", "VSCodium", True),
    ("subl", "Sublime Text", True),
    ("zed", "Zed", True),
    ("gedit", "gedit", True),
    ("gnome-text-editor", "GNOME Text Editor", True),
    ("kate", "Kate", True),
)
GUI_EDITORS = {cmd for cmd, _label, gui in KNOWN_EDITORS if gui}


def available_editors() -> list[tuple[str, str]]:
    """Nainstalované známé editory jako (příkaz, popisek) – pro výběr v Nastavení."""
    return [
        (cmd, f"{label} ({cmd})" + (_(" · okno") if gui else ""))
        for cmd, label, gui in KNOWN_EDITORS
        if shutil.which(cmd)
    ]


def resolve_editor(editor: str | None) -> tuple[str, str]:
    """Jaký editor se spustí a proč: (příkaz, odkud). Pořadí: Nastavení, $VISUAL, $EDITOR, vi."""
    if editor:
        return editor, _("Nastavení")
    for var in ("VISUAL", "EDITOR"):
        if value := os.environ.get(var):
            return value, f"${var}"
    return "vi", _("výchozí")


def editor_installed(editor: str) -> bool:
    try:
        parts = shlex.split(editor)
    except ValueError:
        return False
    return bool(parts) and shutil.which(parts[0]) is not None


def is_gui_editor(cmd: list[str]) -> bool:
    return bool(cmd) and Path(cmd[0]).name in GUI_EDITORS


def editor_command(editor: str | None, path: Path, line: int | None) -> list[str]:
    cmd = shlex.split(resolve_editor(editor)[0])
    name = Path(cmd[0]).name
    if line and name in {
        "vi",
        "vim",
        "nvim",
        "nano",
        "micro",
        "kak",
        "helix",
        "hx",
        "emacs",
        "emacsclient",
        "gedit",
    }:
        if name in {"hx", "helix"}:
            return [*cmd, f"{path}:{line}"]
        return [*cmd, f"+{line}", str(path)]
    if line and name in {"code", "codium"}:
        return [*cmd, "--goto", f"{path}:{line}"]
    if line and name in {"subl", "zed"}:
        return [*cmd, f"{path}:{line}"]
    if line and name == "kate":
        return [*cmd, "--line", str(line), str(path)]
    return [*cmd, str(path)]


def open_in_editor_detached(cmd: list[str]) -> None:
    """Grafický editor: spustit na pozadí, TUI běží dál."""
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def open_url(url: str) -> bool:
    opener = shutil.which("xdg-open")
    if not opener or not url.startswith(("https://", "http://")):
        return False
    subprocess.Popen(
        [opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    return True


def wl_copy(text: str) -> bool:
    exe = shutil.which("wl-copy")
    if not exe:
        return False
    try:
        subprocess.run([exe], input=text.encode(), timeout=3, check=True)
    except (subprocess.SubprocessError, OSError):
        return False
    return True
