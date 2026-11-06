"""Hlavička „vitální funkce“: počty, zdraví, tep a průběh skenu."""

from __future__ import annotations

import random
from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from repo_doctor.i18n import _
from repo_doctor.models import ScanResult, Severity
from repo_doctor.notify import RunState
from repo_doctor.scanner import aggregate_pulse
from repo_doctor.scoring import gauge, sparkline
from repo_doctor.tui.theme import Palette

# Braille body: [sloupec][řádek] -> bit; v jedné buňce je tak 2×4 podbuněk pro „tečky“.
_BRAILLE = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
_TICKS_PER_CELL = 2


def scan_dots(width: int, frame: int) -> list[int]:
    """Masky braille teček pro každou buňku pruhu; tečky letí zprava doleva, každá po své dráze.

    Dráhy (řádek, rychlost, posun) jsou pevně dané šířkou pruhu, takže poloha je čistá funkce
    snímku – v jednom sloupci jich tak může být víc, jinde žádná.
    """
    rng = random.Random(width)  # noqa: S311 – vzhled animace, ne kryptografie
    span = width * _TICKS_PER_CELL
    cells = [0] * width
    for _lane in range(max(3, width * 2 // 3)):
        row = rng.randrange(4)
        speed = rng.randint(1, 3)
        x = (rng.randrange(span) - speed * frame) % span
        cells[x // _TICKS_PER_CELL] |= _BRAILLE[x % _TICKS_PER_CELL][row]
    return cells


def scan_bar(width: int, done: int, total: int, frame: int, p: Palette) -> Text:
    """Ukazatel průběhu skenu: vyplněná část je jednolitě zelená, tečky letí jen v pravé části.

    Animace nesmí prostupovat do zeleného pruhu, který se posouvá zleva doprava (#89).
    """
    filled = round(width * done / total) if total else 0
    t = Text()
    for i, mask in enumerate(scan_dots(width, frame)):
        if i < filled:
            t.append(" ", style=Style(bgcolor=p.ok))
            continue
        char = chr(0x2800 + mask) if mask else " "
        t.append(char, style=Style(color=p.muted, bgcolor=p.dim))
    return t


def vitals_text(
    result: ScanResult,
    p: Palette,
    width: int,
    *,
    no_pulse_days: int,
    scanning: bool,
    done: int,
    total: int,
    current: str,
    keys: tuple[str, str] = ("R", "x"),
    scheduled: RunState | None = None,
    frame: int = 0,
) -> Text:
    s = result.summary(no_pulse_days)
    t = Text()
    t.append(_("{repos} repozitářů").format(repos=s.repos), style=Style(color=p.text, bold=True))
    wide = width >= 96
    gap = "    " if wide else "  "
    t.append(gap)
    for sev in Severity:
        n = s.by_severity[sev]
        style = Style(color=p.severity(sev), bold=True) if n else Style(color=p.dim)
        t.append(f"{sev.symbol} {sev.short} {n}", style=style)
        t.append("   " if wide else "  ")
    t.append("  " if wide else "")
    t.append(_("zdraví "), style=Style(color=p.muted))
    blocks = 10 if width >= 90 else 5
    t.append(gauge(s.health, blocks), style=Style(color=p.score(s.health)))
    t.append(f" {s.health}", style=Style(color=p.score(s.health), bold=True))
    pulse_w = max(0, min(12, width - t.cell_len - 10))
    if pulse_w >= 6:
        t.append("    " if wide else "  ")
        t.append(_("tep "), style=Style(color=p.muted))
        t.append(sparkline(aggregate_pulse(result), pulse_w), style=Style(color=p.accent))
    t.append("\n")
    if scanning:
        bar_w = 20 if width >= 90 else 10
        t.append(_("sken "), style=Style(color=p.muted))
        t.append_text(scan_bar(bar_w, done, total, frame, p))
        t.append(f" {done}/{total}", style=Style(color=p.text))
        if current:
            t.append("   › ", style=Style(color=p.muted))
            t.append(current, style=Style(color=p.text))
        t.append(_("   {v} zruší sken").format(v=keys[1]), style=Style(color=p.dim))
    else:
        when = result.finished_at
        if when is not None:
            local = when.astimezone()
            dur = (when - result.started_at).total_seconds()
            t.append(
                _("poslední sken {local:%-d. %-m. %H:%M}").format(local=local),
                style=Style(color=p.muted),
            )
            t.append(f" · {dur:.0f} s", style=Style(color=p.dim))
        else:
            t.append(_("zatím bez skenu"), style=Style(color=p.muted))
        if result.cancelled:
            t.append(_(" · přerušený"), style=Style(color=p.med))
        if result.warnings:
            t.append(
                _(" · ⚠ {len} upozornění").format(len=len(result.warnings)),
                style=Style(color=p.med),
            )
        if scheduled is not None:
            try:
                at = datetime.fromisoformat(scheduled.ts).astimezone()
                label = at.strftime(_("%-d. %-m. %H:%M"))
            except ValueError:
                label = "?"
            t.append(_(" · plánovaný {label}").format(label=label), style=Style(color=p.muted))
            t.append(
                _(" ⚠ změny") if scheduled.changes else _(" beze změn"),
                style=Style(color=p.med if scheduled.changes else p.dim),
            )
    t.truncate(width * 2, overflow="ellipsis")
    lines = t.split("\n")
    for line in lines:
        line.truncate(width, overflow="ellipsis")
    return Text("\n").join(lines)


class Vitals(Static):
    DEFAULT_CSS = """
    Vitals { height: 4; border: round $accent; padding: 0 1; border-title-color: $accent; }
    """
