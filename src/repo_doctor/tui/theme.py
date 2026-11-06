"""Paleta „Triáž“ – jediné téma: černé pozadí, obrysy a texty v zelené #00aa00."""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text
from textual.theme import Theme

from repo_doctor.models import Severity


@dataclass(frozen=True)
class Palette:
    background: str
    panel: str
    selection: str
    text: str
    muted: str
    dim: str  # potlačený (nuly)
    border: str
    accent: str
    high: str
    med: str
    low: str
    ok: str
    diff_add: str
    diff_del: str

    def severity(self, sev: Severity) -> str:
        return {Severity.HIGH: self.high, Severity.MEDIUM: self.med, Severity.LOW: self.low}[sev]

    def score(self, score: int | None) -> str:
        if score is None:
            return self.muted
        return self.high if score < 40 else self.med if score < 70 else self.ok


# Jediné téma (#89): černé pozadí a základní zelená #00aa00 pro obrysy a texty.
# Barvy severit zůstávají rozlišitelné (červená / jantarová / modrá), jinak by se z nálezů
# nedalo poznat, co hoří. Všechny texty mají na černé kontrast ≥ 4.5:1 (WCAG AA).
BASE_GREEN = "#00aa00"
PALETTE = Palette(
    background="#000000",
    panel="#000000",
    selection="#003300",
    text=BASE_GREEN,
    muted="#008a00",
    dim="#004d00",
    border="#006600",
    accent=BASE_GREEN,
    high="#ff5f6d",
    med="#f5b642",
    low="#6fa8ff",
    ok=BASE_GREEN,
    diff_add="#062a0a",
    diff_del="#2b1719",
)

THEME_NAME = "repo-doctor"


def _theme(name: str, p: Palette) -> Theme:
    return Theme(
        name=name,
        primary=p.accent,
        secondary=p.low,
        accent=p.accent,
        warning=p.med,
        error=p.high,
        success=p.ok,
        foreground=p.text,
        background=p.background,
        surface=p.panel,
        panel=p.panel,
        boost=p.selection,
        dark=True,
        variables={
            "rd-muted": p.muted,
            "rd-dim": p.dim,
            "rd-border": p.border,
            "rd-selection": p.selection,
            "rd-high": p.high,
            "rd-med": p.med,
            "rd-low": p.low,
            "rd-ok": p.ok,
            "block-cursor-background": p.selection,
            "block-cursor-foreground": p.text,
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": p.selection,
            "block-cursor-blurred-foreground": p.text,
            "block-hover-background": p.selection,
            "footer-background": p.background,
            "footer-key-foreground": p.background,
            "footer-key-background": p.accent,
            "footer-description-foreground": p.muted,
            "footer-item-background": p.background,
            "input-selection-background": p.selection,
            "scrollbar": p.border,
            "scrollbar-background": p.panel,
            "border": p.accent,
            "border-blurred": p.border,
        },
    )


THEME = _theme(THEME_NAME, PALETTE)


def sev_text(p: Palette, sev: Severity, count: int | None = None, *, width: int = 2) -> Text:
    if count is None:
        return Text(f"{sev.symbol} {sev.short}", style=Style(color=p.severity(sev), bold=True))
    style = Style(color=p.severity(sev)) if count else Style(color=p.dim)
    return Text(f"{count:>{width}}", style=style)
