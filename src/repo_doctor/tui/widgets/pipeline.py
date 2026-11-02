"""Průběh CI jako graf jobů po vzoru GitHub Actions (uzly ve sloupcích, spojnice, kroky).

Widget jen kreslí hotové `pipeline.Graph`; zdroj dat a závislosti řeší `repo_doctor.pipeline`.
Ikony a barvy odpovídají GitHub Actions: ✓ prošel, ✗ selhal, točící se spinner běží,
○ čeká, ⊘ přeskočen nebo zrušen. Vybraný job se rozbalí na kroky (⏎).
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual.geometry import Region
from textual.widget import Widget

from repo_doctor import pipeline
from repo_doctor.forges.base import CIJob, JobState
from repo_doctor.i18n import _
from repo_doctor.tui.util import printable

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp

SPINNER = "◐◓◑◒"
MIN_WIDTH, MAX_WIDTH = 16, 40
# směry spojnic: nahoru, doprava, dolů, doleva
N, E, S, W = 1, 2, 4, 8
LINES = {
    N: "│", S: "│", N | S: "│", E: "─", W: "─", E | W: "─",
    N | E: "╰", N | W: "╯", S | E: "╭", S | W: "╮",
    N | S | E: "├", N | S | W: "┤", E | W | S: "┬", E | W | N: "┴", N | E | S | W: "┼",
}  # fmt: skip

Cell = tuple[str, Style | None]
Key = tuple[int, int]  # (graf, job v grafu)


def duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def elapsed(started: datetime | None, completed: datetime | None, now: datetime) -> str:
    if started is None:
        return ""
    return duration(((completed or now) - started).total_seconds())


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


class PipelineView(Widget, can_focus=True):
    """Graf jobů; šipky a j/k vybírají, ⏎ rozbalí kroky jobu."""

    DEFAULT_CSS = "PipelineView { width: auto; height: auto; }"
    BINDINGS = [
        ("up", "cursor_up", ""),
        ("down", "cursor_down", ""),
        ("left", "cursor_left", ""),
        ("right", "cursor_right", ""),
    ]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.graphs: list[pipeline.Graph] = []
        self.selected: Key | None = None
        self.expanded: set[tuple[str, str]] = set()
        self.frame = 0
        self.rects: dict[Key, Region] = {}
        self._text = Text("")

    @property
    def rd(self) -> RepoDoctorApp:
        return self.app  # type: ignore[return-value]

    # ------------------------------------------------------------------ data
    @staticmethod
    def _job_key(job: CIJob) -> tuple[str, str]:
        return (job.workflow, job.name)

    def on_mount(self) -> None:
        self.set_interval(0.25, self._tick)

    def _tick(self) -> None:
        if any(j.state == "running" for g in self.graphs for j in g.jobs):
            self.frame += 1
            self._rebuild()

    def set_jobs(self, jobs: list[CIJob]) -> None:
        """Nový stav CI; výběr a rozbalené joby se drží podle názvu."""
        keep = self.selected_job
        self.graphs = pipeline.layout(jobs)
        self.selected = None
        if keep is not None:
            for gi, g in enumerate(self.graphs):
                for ji, job in enumerate(g.jobs):
                    if self._job_key(job) == self._job_key(keep):
                        self.selected = (gi, ji)
        if self.selected is None and self._order():
            self.selected = self._order()[0]
        self._rebuild()

    @property
    def selected_job(self) -> CIJob | None:
        if self.selected is None or self.selected[0] >= len(self.graphs):
            return None
        g = self.graphs[self.selected[0]]
        return g.jobs[self.selected[1]] if self.selected[1] < len(g.jobs) else None

    def _order(self) -> list[Key]:
        return [(gi, ji) for gi, g in enumerate(self.graphs) for col in g.columns for ji in col]

    # ------------------------------------------------------------------ pohyb
    def _move(self, step: int) -> None:
        order = self._order()
        if not order:
            return
        i = order.index(self.selected) if self.selected in order else 0
        self.selected = order[max(0, min(len(order) - 1, i + step))]
        self._rebuild()
        self._reveal()

    def action_cursor_down(self) -> None:
        self._move(1)

    def action_cursor_up(self) -> None:
        self._move(-1)

    def _side(self, delta: int) -> None:
        if self.selected is None or self.selected not in self.rects:
            return
        gi, ji = self.selected
        g = self.graphs[gi]
        col = g.column_of(ji) + delta
        if not 0 <= col < len(g.columns):
            return
        y = self.rects[self.selected].y
        best = min(g.columns[col], key=lambda k: abs(self.rects[(gi, k)].y - y))
        self.selected = (gi, best)
        self._rebuild()
        self._reveal()

    def action_cursor_left(self) -> None:
        self._side(-1)

    def action_cursor_right(self) -> None:
        self._side(1)

    def toggle(self) -> None:
        job = self.selected_job
        if job is None or not job.steps:
            return
        self.expanded ^= {self._job_key(job)}
        self._rebuild()
        self._reveal()

    def _reveal(self) -> None:
        region = self.rects.get(self.selected) if self.selected else None
        parent = self.parent
        if region is not None and isinstance(parent, Widget):
            self.call_after_refresh(parent.scroll_to_region, region, animate=False)

    # ------------------------------------------------------------------ kreslení
    def _rebuild(self) -> None:
        self._text, self.rects, width, height = self._draw()
        self.styles.width = max(1, width)
        self.styles.height = max(1, height)
        self.refresh(layout=True)

    def render(self) -> Text:
        return self._text

    def _icon(self, state: JobState) -> tuple[str, str]:
        p = self.rd.palette
        return {
            "success": ("✓", p.ok),
            "failure": ("✗", p.high),
            "running": (SPINNER[self.frame % len(SPINNER)], p.med),
            "queued": ("○", p.muted),
            "skipped": ("⊘", p.muted),
            "cancelled": ("⊘", p.muted),
        }[state]

    def _draw(self) -> tuple[Text, dict[Key, Region], int, int]:
        p, now = self.rd.palette, datetime.now(UTC)
        cells: dict[tuple[int, int], Cell] = {}
        masks: dict[tuple[int, int], int] = {}
        rects: dict[Key, Region] = {}

        def put(x: int, y: int, text: str, style: Style | None = None) -> None:
            for i, ch in enumerate(printable(text)):  # názvy jobů a kroků dodává hosting
                cells[(x + i, y)] = (ch, style)

        def link(a: tuple[int, int], b: tuple[int, int]) -> None:
            (ax, ay), (bx, by) = a, b
            bit = E if bx > ax else W if bx < ax else S if by > ay else N
            back = {E: W, W: E, S: N, N: S}[bit]
            masks[a] = masks.get(a, 0) | bit
            masks[b] = masks.get(b, 0) | back

        dim, muted = Style(color=p.dim), Style(color=p.muted)
        y = 0
        for gi, g in enumerate(self.graphs):
            if g.title:
                put(0, y, g.title, Style(color=p.accent, bold=True))
                y += 1
            if any(g.column_titles):
                y += 1  # řádek se jmény stages
            top = y
            widths: list[int] = []
            heights: dict[int, int] = {}
            for col in g.columns:
                need = MIN_WIDTH
                for ji in col:
                    job = g.jobs[ji]
                    dur = elapsed(job.started, job.completed, now)
                    need = max(need, len(job.name) + len(dur) + 8)
                    if self._job_key(job) in self.expanded:
                        need = max(need, *(len(st.name) + 14 for st in job.steps), 0)
                widths.append(min(MAX_WIDTH, need))
            long_for = {d for _s, d in g.long_edges}
            for col in g.columns:
                for ji in col:
                    job = g.jobs[ji]
                    rows = 3 + (1 if ji in long_for else 0)
                    if self._job_key(job) in self.expanded:
                        rows += len(job.steps)
                    heights[ji] = rows
            gaps = []
            for c in range(len(g.columns) - 1):
                targets = {d for _s, d in g.edges if g.column_of(d) == c + 1}
                gaps.append(max(3, len(targets) + 2))
            col_x = [0]
            for c, w in enumerate(widths[:-1]):
                col_x.append(col_x[-1] + w + gaps[c])
            title_row: dict[int, int] = {}
            height_total = top
            for c, col in enumerate(g.columns):
                if g.column_titles[c]:
                    put(
                        col_x[c],
                        top - 1,
                        fit(g.column_titles[c], widths[c]),
                        Style(color=p.muted, bold=True),
                    )
                cy = top
                for ji in col:
                    job = g.jobs[ji]
                    selected = self.selected == (gi, ji)
                    border = Style(color=p.accent, bold=True) if selected else dim
                    x, w, h = col_x[c], widths[c], heights[ji]
                    rects[(gi, ji)] = Region(x, cy, w, h)
                    icon, color = self._icon(job.state)
                    put(x, cy, "╭" + "─" * (w - 2) + "╮", border)
                    put(x, cy + h - 1, "╰" + "─" * (w - 2) + "╯", border)
                    for r in range(1, h - 1):
                        put(x, cy + r, "│", border)
                        put(x + w - 1, cy + r, "│", border)
                    dur = elapsed(job.started, job.completed, now)
                    inner = w - 4
                    name = fit(job.name, inner - 2 - (len(dur) + 1 if dur else 0))
                    put(x + 2, cy + 1, icon, Style(color=color, bold=True))
                    put(x + 4, cy + 1, name, Style(color=p.text, bold=selected))
                    if dur:
                        put(x + w - 2 - len(dur), cy + 1, dur, muted)
                    title_row[ji] = cy + 1
                    row = cy + 2
                    if ji in long_for:
                        after = ", ".join(g.jobs[s].name for s, d in g.long_edges if d == ji)
                        put(x + 2, row, fit(_("↳ po {need}").format(need=after), inner), dim)
                        row += 1
                    if self._job_key(job) in self.expanded:
                        for st in job.steps:
                            sicon, scolor = self._icon(st.state)
                            sdur = elapsed(st.started, st.completed, now)
                            put(x + 3, row, sicon, Style(color=scolor))
                            put(
                                x + 5,
                                row,
                                fit(st.name, inner - 3 - (len(sdur) + 1 if sdur else 0)),
                                Style(color=p.text),
                            )
                            if sdur:
                                put(x + w - 2 - len(sdur), row, sdur, muted)
                            row += 1
                    cy += h + 1
                height_total = max(height_total, cy - 1)
            # spojnice mezi sousedními sloupci
            for c in range(len(g.columns) - 1):
                chans = sorted(
                    {d for _s, d in g.edges if g.column_of(d) == c + 1}, key=lambda d: title_row[d]
                )
                gx = col_x[c] + widths[c]
                for s, d in g.edges:
                    if g.column_of(d) != c + 1:
                        continue
                    chx = gx + 1 + chans.index(d)
                    rs, rd = title_row[s], title_row[d]
                    path = [(x, rs) for x in range(gx, chx + 1)]
                    step = 1 if rd >= rs else -1
                    path += [(chx, yy) for yy in range(rs + step, rd + step, step)]
                    path += [(x, rd) for x in range(chx + 1, gx + gaps[c])]
                    for a, b in pairwise(path):
                        link(a, b)
                    cells[(col_x[c] + widths[c] - 1, rs)] = (
                        "├",
                        cells[(col_x[c] + widths[c] - 1, rs)][1],
                    )
                    masks[path[-1]] = masks.get(path[-1], 0)
                    cells[path[-1]] = ("▶", dim)
            y = height_total + 2
        for pos, mask in masks.items():
            if pos not in cells:
                cells[pos] = (LINES.get(mask, "┼"), dim)
        width = max((x for x, _y in cells), default=0) + 1
        height = max((yy for _x, yy in cells), default=0) + 1
        lines: list[Text] = []
        for row in range(height):
            line = Text(no_wrap=True)
            run, run_style = "", None
            for cx in range(width):
                ch, style = cells.get((cx, row), (" ", None))
                if style != run_style and run:
                    line.append(run, style=run_style)
                    run = ""
                run, run_style = run + ch, style
            line.append(run, style=run_style)
            lines.append(line)
        return Text("\n").join(lines), rects, width, height
