"""Řádky triáže: proužek, měřič skóre, počty severit, git stav, hosting a tep."""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

from repo_doctor.czech import more
from repo_doctor.i18n import _
from repo_doctor.models import RepoResult, Severity
from repo_doctor.scoring import Band, band, gauge, score_label, sparkline
from repo_doctor.tui.theme import Palette
from repo_doctor.tui.triage_model import Group
from repo_doctor.tui.util import git_state, pad

BAND_COLOR = {
    Band.HIGH: "high",
    Band.MEDIUM: "med",
    Band.LOW: "low",
    Band.NONE: "ok",
    Band.UNCHECKED: "muted",
}

PULSE_W = 12
GIT_W = 10
HOST_W = 11
COUNTS_W = 8  # "12 12 12"


@dataclass(frozen=True)
class Columns:
    width: int
    show_pulse: bool
    show_host: bool

    @property
    def name_w(self) -> int:
        fixed = 1 + 1 + 3 + 1 + 5 + 2 + 1 + COUNTS_W + 3 + GIT_W
        if self.show_host:
            fixed += 1 + HOST_W
        if self.show_pulse:
            fixed += 1 + PULSE_W + 1
        return max(8, self.width - fixed)


def header_text(cols: Columns, p: Palette) -> Text:
    style = Style(color=p.muted, bold=True)
    t = Text(" " * 2, style=style)
    t.append(pad(_("SKÓRE"), 9))
    t.append("  ")
    t.append(pad(_("REPOZITÁŘ"), cols.name_w, style))
    t.append(" ")
    for sev in Severity:
        t.append(f"{sev.symbol:>2}", style=Style(color=p.severity(sev), bold=True))
        if sev is not Severity.LOW:
            t.append(" ")
    t.append("   ")
    t.append(pad("GIT", GIT_W, style))
    if cols.show_host:
        t.append(" ")
        t.append(pad("HOSTING", HOST_W, style))
    if cols.show_pulse:
        t.append(" ")
        t.append(pad(_("TEP · 30 DNÍ"), PULSE_W, style))
    t.stylize(style, 0, 11)
    return t


def repo_row(repo: RepoResult, cols: Columns, p: Palette) -> Text:
    repo_band = band(repo)
    color = getattr(p, BAND_COLOR[repo_band])  # barva skóre = barva skupiny (#73)
    t = Text()
    t.append("▌", style=Style(color=color))
    t.append(f"{score_label(repo.score):>3} ", style=Style(color=color, bold=True))
    t.append(gauge(repo.score), style=Style(color=color))
    t.append("  ")
    t.append(pad(repo.name, cols.name_w, Style(color=p.text, bold=repo_band is Band.HIGH)))
    t.append(" ")
    counts = repo.counts()
    for sev in Severity:
        n = counts[sev]
        t.append(f"{n:>2}", style=Style(color=p.severity(sev)) if n else Style(color=p.dim))
        if sev is not Severity.LOW:
            t.append(" ")
    t.append("   ")
    state = git_state(repo)
    state_style = (
        Style(color=p.muted)
        if state in (_("čisté"), _("archiv"))
        else Style(color=p.med if _("bez remote") not in state else p.high)
    )
    t.append(pad(state, GIT_W, state_style))
    if cols.show_host:
        t.append(" ")
        if repo.visibility == "public":
            vis = Text("◉ ", style=Style(color=p.high))
        elif repo.visibility == "private":
            vis = Text("○ ", style=Style(color=p.muted))
        elif repo.forge:
            vis = Text("· ", style=Style(color=p.dim))
        else:
            vis = Text("  ")
        vis.append(repo.forge or "—", style=Style(color=p.text if repo.forge else p.dim))
        t.append(pad(vis, HOST_W))
    if cols.show_pulse:
        t.append(" ")
        t.append(
            pad(
                sparkline(repo.state.pulse, PULSE_W) or "▁" * PULSE_W,
                PULSE_W,
                Style(color=p.accent if sum(repo.state.pulse) else p.dim),
            )
        )
        t.append(" ")
    return t


def group_header(group: Group, width: int, p: Palette, *, collapsed: bool, toggle_key: str) -> Text:
    color = getattr(p, BAND_COLOR[group.band]) if group.band else p.accent
    label = f"── {group.label} · {len(group.repos)}"
    if group.note:
        label += f" {group.note}"
    if collapsed:
        label += _(" sbaleno · {toggle_key} rozbalí").format(toggle_key=toggle_key)
    label += " "
    t = Text(label, style=Style(color=color, bold=True))
    t.append("─" * max(0, width - len(label) - 1), style=Style(color=p.border))
    return t


def preview_text(repo: RepoResult | None, width: int, p: Palette, limit: int = 4) -> Text:
    title = _("── NÁHLED · {name} ").format(name=repo.name) if repo else _("── NÁHLED ")
    t = Text(title, style=Style(color=p.accent, bold=True))
    t.append("─" * max(0, width - len(title)), style=Style(color=p.border))
    if repo is None:
        t.append(_("\n  vyber repo (j/k)"), style=Style(color=p.muted))
        return t
    findings = sorted(repo.findings, key=lambda f: (-f.severity.rank, f.check_id))
    if not findings:
        t.append(_("\n  ✓ bez nálezů"), style=Style(color=p.ok))
        return t
    id_w = min(24, max(len(f.check_id) for f in findings[:limit]) + 1)
    for f in findings[:limit]:
        line = Text("\n  ")
        line.append(f"{f.severity.symbol} ", style=Style(color=p.severity(f.severity)))
        line.append(pad(f.check_id, id_w, Style(color=p.text)))
        line.append(" ")
        msg = Text(f.message, style=Style(color=p.muted))
        if f.fixable:
            msg.append(_("   ✓ opravitelné"), style=Style(color=p.ok))
        line.append(pad(msg, max(4, width - id_w - 6)))
        t.append(line)
    if len(findings) > limit:
        t.append(
            "\n    " + more(len(findings) - limit),
            style=Style(color=p.muted),
        )
    return t
