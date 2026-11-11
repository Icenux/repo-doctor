"""Karta repa: strom nálezů podle kategorií a diagnóza vybraného nálezu."""

from __future__ import annotations

import contextlib
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Static, Tree

from repo_doctor import history
from repo_doctor.checkdocs import load as load_doc
from repo_doctor.cleanup import human
from repo_doctor.config import AllowEntry, ConfigError
from repo_doctor.czech import commits, files
from repo_doctor.i18n import _
from repo_doctor.models import Category, Finding, RepoResult
from repo_doctor.scoring import gauge, score_label, sparkline
from repo_doctor.tui.bindings import bindings
from repo_doctor.tui.screens.base import BaseScreen
from repo_doctor.tui.screens.dialogs import InputDialog
from repo_doctor.tui.util import (
    editor_command,
    git_state,
    home_path,
    is_gui_editor,
    open_in_editor_detached,
    open_url,
    relative_time,
    wl_copy,
)
from repo_doctor.tui.widgets.keybar import KeyBar
from repo_doctor.tui.widgets.panel import split_title

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp
    from repo_doctor.tui.theme import Palette

CATEGORY_ORDER = (Category.SECURITY, Category.FORGE, Category.MAINTENANCE, Category.GIT)


def card_header(repo: RepoResult, app: RepoDoctorApp, width: int) -> Text:
    p = app.palette
    muted = Style(color=p.muted)
    t = Text()
    t.append(_("SKÓRE "), style=muted)
    color = p.score(repo.score)
    t.append(f"{score_label(repo.score)}/100 ", style=Style(color=color, bold=True))
    t.append(gauge(repo.score), style=Style(color=color))
    if repo.untrusted_owner:
        t.append(_("    nelze zkontrolovat"), style=Style(color=p.med, bold=True))
        t.append(
            _("\ngit repo odmítl – patří jinému uživateli (safe.directory). Postup je v nálezu."),
            style=Style(color=p.text),
        )
        return t
    t.append(_("    větev "), style=muted)
    t.append(
        repo.state.branch or ("detached" if repo.state.detached else "—"), style=Style(color=p.text)
    )
    t.append("    remote ", style=muted)
    remote = repo.remotes[0] if repo.remotes else None
    if remote:
        label = f"{remote.forge or remote.host or ''}:{remote.owner_path or ''}".strip(":")
        t.append(label or remote.url, style=Style(color=p.text))
    else:
        t.append(_("žádný (bez zálohy)"), style=Style(color=p.high))
    t.append("   ")
    if repo.visibility == "public":
        t.append(_("◉ veřejné"), style=Style(color=p.high, bold=True))
    elif repo.visibility == "private":
        t.append(_("○ privátní"), style=muted)
    else:
        t.append(_("· viditelnost neznámá"), style=Style(color=p.dim))
    t.append(_("\nTEP  "), style=muted)
    pulse_w = max(10, min(30, width - 50))
    t.append(
        sparkline(repo.state.pulse, pulse_w) or "▁" * pulse_w,
        style=Style(color=p.accent if sum(repo.state.pulse) else p.dim),
    )
    t.append(
        _("   poslední commit {relative_time}").format(
            relative_time=relative_time(repo.state.last_commit, app.now())
        ),
        style=muted,
    )
    t.append(f" · {git_state(repo)}", style=Style(color=p.text))
    t.append("\n")
    info = repo.forge_info
    if "ci" in info:
        ci = info.get("ci")
        if ci == "failure":
            t.append(
                _("CI ✗ selhalo #{v}").format(v=info.get("ci_number") or "?"),
                style=Style(color=p.high),
            )
        elif ci == "success":
            t.append(_("CI ✓ prošlo"), style=Style(color=p.ok))
        else:
            t.append(f"CI {ci or '—'}", style=muted)
        prot = info.get("protected")
        t.append(_("    ochrana větve "), style=muted)
        t.append(
            "✓" if prot else "✗" if prot is False else "?",
            style=Style(color=p.ok if prot else p.high if prot is False else p.dim),
        )
        alerts = info.get("alerts")
        t.append(_("    alerty {v}").format(v=alerts if alerts is not None else "?"), style=muted)
        t.append(_("    otevřené PR {get}").format(get=info.get("open_items", 0)), style=muted)
    elif repo.skipped.get("forge-ci-failing"):
        t.append(f"hosting: {repo.skipped['forge-ci-failing']}", style=Style(color=p.dim))
    else:
        t.append(_("hosting: bez dat"), style=Style(color=p.dim))
    if "fetch" in repo.errors:
        t.append(_("\nfetch ✗ "), style=Style(color=p.high))
        t.append(repo.errors["fetch"], style=Style(color=p.text))
        if "fetch_detail" in repo.errors:
            t.append(
                _("  (git: {v})").format(v=repo.errors["fetch_detail"]), style=Style(color=p.dim)
            )
    wrap = app.size.height >= WRAP_MIN_HEIGHT  # na nízkém terminálu zbude místo na nálezy
    for label, items in detail_items(repo, app):
        for line in layout_items(label, items, max(30, width - 4), wrap=wrap, p=p):
            t.append("\n")
            t.append_text(line)
    return t


def _date(when: datetime) -> str:
    return when.astimezone().strftime(_("%-d. %-m. %Y"))


LABEL_W = 13  # nejdelší popisek (DEPENDENCIES) + mezera
WRAP_MIN_HEIGHT = 32


def layout_items(
    label: str, items: list[Text], width: int, *, wrap: bool, p: Palette
) -> list[Text]:
    """Popisek a položky oddělené „ · “; zalomí se mezi položkami, jinak se uřízne."""
    sep = Text(" · ", style=Style(color=p.dim))
    lines = [Text(label.ljust(LABEL_W), style=Style(color=p.muted))]
    for item in items:
        line = lines[-1]
        first = line.cell_len <= LABEL_W
        if wrap and not first and line.cell_len + sep.cell_len + item.cell_len > width:
            line = Text(" " * LABEL_W)
            lines.append(line)
            first = True
        if not first:
            line.append_text(sep)
        line.append_text(item)
    for line in lines:
        line.truncate(width, overflow="ellipsis")
    return lines


def detail_items(repo: RepoResult, app: RepoDoctorApp) -> list[tuple[str, list[Text]]]:
    """Řádky KÓD / ZÁVISLOSTI / HISTORIE / NA HOSTINGU z `repo.details` (#36); prázdné vynechá."""
    p = app.palette
    d = repo.details
    label, text = Style(color=p.muted), Style(color=p.text)

    def item(*parts: tuple[str, Style]) -> Text:
        return Text.assemble(*parts)

    result: list[tuple[str, list[Text]]] = []
    code: list[Text] = []
    total = sum(lang.bytes for lang in d.languages)
    for lang in d.languages:
        share = lang.bytes * 100 / total if total else 0
        code.append(item((lang.name, text), (f" {share:.0f} %" if share >= 1 else " <1 %", label)))
    if d.files is not None:
        code.append(item((files(d.files), label)))
    if d.worktree_bytes is not None:
        code.append(item((human(d.worktree_bytes), label)))
    if d.git_bytes is not None:
        code.append(item((f".git {human(d.git_bytes)}", label)))
    if d.license:
        code.append(item((_("licence "), label), (d.license, text)))
    if code:
        result.append((_("KÓD"), code))

    deps = []
    for eco in d.ecosystems:
        managers = f" ({', '.join(eco.managers)})" if eco.managers else ""
        counts = (
            _(" {direct} přímých / {total} celkem").format(direct=eco.direct, total=eco.total)
            if eco.total or eco.direct
            else ""
        )
        deps.append(item((f"{eco.name}{managers}", text), (counts, label)))
    if deps:
        result.append((_("ZÁVISLOSTI"), deps))

    hist: list[Text] = []
    if d.first_commit is not None:
        hist.append(item((_("od "), label), (_date(d.first_commit), text)))
    if n := repo.state.commit_count:
        hist.append(item((commits(n), text)))
    if d.commits_30d is not None:
        hist.append(item((_("{commits_30d} za 30 dní").format(commits_30d=d.commits_30d), label)))
    if d.commits_year is not None:
        hist.append(item((_("{commits_year} za rok").format(commits_year=d.commits_year), label)))
    if d.authors:
        top = ", ".join(f"{a.name} {a.commits}" for a in d.top_authors)
        hist.append(
            item(
                (_("autoři {authors}").format(authors=d.authors), text),
                (f" ({top})" if top else "", label),
            )
        )
    if d.branches is not None:
        hist.append(item((_("větve {branches}").format(branches=d.branches), text)))
    if d.last_tag:
        when = f" ({_date(d.last_tag_date)})" if d.last_tag_date else ""
        hist.append(item((f"tag {d.last_tag}", text), (when, label)))
    if hist:
        result.append((_("HISTORIE"), hist))

    forge: list[Text] = []
    if d.forge_description:
        forge.append(item((d.forge_description, text)))
    if d.forge_topics:
        forge.append(item((_("témata "), label), (", ".join(d.forge_topics), text)))
    if d.forge_stars is not None:
        forge.append(item((f"★ {d.forge_stars}", text)))
    if d.forge_forks is not None:
        forge.append(item((_("forky {forge_forks}").format(forge_forks=d.forge_forks), text)))
    if d.forge_open_issues is not None:
        forge.append(item((f"issues {d.forge_open_issues}", text)))
    if d.forge_created is not None:
        forge.append(item((_("vytvořeno {date}").format(date=_date(d.forge_created)), label)))
    if d.forge_pushed is not None:
        forge.append(item((f"push {relative_time(d.forge_pushed, app.now())}", label)))
    if forge:
        result.append((_("NA HOSTINGU"), forge))
    return result


def diagnosis(finding: Finding | None, app: RepoDoctorApp) -> Text:
    p = app.palette
    muted = Style(color=p.muted)
    if finding is None:
        return Text(_("Vyber nález vlevo (j/k)."), style=muted)
    t = Text()
    t.append(
        f"{finding.severity.symbol} {finding.severity.short}",
        style=Style(color=p.severity(finding.severity), bold=True),
    )
    t.append(f" · {finding.check_id}\n", style=Style(color=p.text, bold=True))
    # název kontroly je msgid – přeloží i nálezy z uloženého skenu v jiném jazyce
    t.append(f"{_(finding.title)}\n\n", style=Style(color=p.text))

    def row(label: str, value: str | Text) -> None:
        t.append(f"{label:<8}", style=muted)
        t.append(value if isinstance(value, Text) else Text(value, style=Style(color=p.text)))
        t.append("\n")

    row(_("nález"), finding.message)
    where = finding.location.render()
    if where:
        row(_("kde"), where)
    if finding.kind:
        row(_("typ"), finding.kind)
    if finding.snippet:
        sn = Text(finding.snippet, style=Style(color=p.med))
        sn.append(_("   odmaskovat nejde · o otevře soubor"), style=Style(color=p.dim))
        row(_("ukázka"), sn)
    for key, value in finding.data.items():
        if key in ("count", "rule"):
            continue
        shown = ", ".join(map(str, value[:8])) if isinstance(value, list) else str(value)
        row(key, shown)
    doc = load_doc(finding.check_id)
    if doc:
        t.append(_("\nPROČ TO VADÍ\n"), style=Style(color=p.accent, bold=True))
        t.append(doc.why + "\n", style=Style(color=p.text))
        t.append(_("\nPOSTUP\n"), style=Style(color=p.accent, bold=True))
        for i, step in enumerate(doc.steps, start=1):
            t.append(f"{i}  ", style=Style(color=p.accent))
            t.append(step.replace("`", "") + "\n", style=Style(color=p.text))
        if doc.note:
            t.append("\n" + doc.note + "\n", style=Style(color=p.med))
    if finding.fixable:
        t.append(
            _("\n✓ opravitelné automaticky – {key} připraví opravu\n").format(
                key=app.key("open_fix")
            ),
            style=Style(color=p.ok),
        )
    t.append(
        _("\n {key}  otevřít soubor   {key2}  na webu   {key3}  kopírovat cestu").format(
            key=app.key("open_editor"), key2=app.key("open_web"), key3=app.key("copy_path")
        ),
        style=Style(color=p.muted),
    )
    return t


class DetailScreen(BaseScreen):
    help_context = "detail"
    BINDINGS = [*BaseScreen.BINDINGS, *bindings("detail")]

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.current: Finding | None = None

    @property
    def rd(self) -> RepoDoctorApp:
        return self.app  # type: ignore[return-value]

    @property
    def repo(self) -> RepoResult | None:
        return self.rd.repo(self.path)

    def compose(self) -> ComposeResult:
        yield Static(id="card-header")
        with Horizontal(id="card-body"):
            with Vertical(id="findings-panel"):
                tree: Tree[Finding | None] = Tree(_("nálezy"), id="findings")
                tree.show_root = False
                tree.guide_depth = 2
                yield tree
                yield Static(id="findings-hint")
            with VerticalScroll(id="diagnosis"):
                yield Static(id="diag")
        yield KeyBar(
            [
                ("esc", _("zpět")),
                ("j/k", _("nález")),
                "open_fix",
                "publish_open",
                "allowlist",
                "open_editor",
                "open_web",
                "copy_path",
                "help",
            ]
        )

    def on_mount(self) -> None:
        self.render_all()
        self.query_one(Tree).focus()

    def on_resize(self, event: Resize) -> None:
        self.render_all()

    def on_theme_changed(self) -> None:
        self.render_all()

    def render_all(self) -> None:
        repo = self.repo
        app = self.rd
        p = app.palette
        header = self.query_one("#card-header", Static)
        if repo is None:
            header.update(Text(_("Repo už není ve výsledcích skenu."), style=Style(color=p.muted)))
            return
        split_title(header, _("karta · {name}").format(name=repo.name), home_path(repo.path))
        header.update(card_header(repo, app, self.app.size.width))
        tree = self.query_one(Tree)
        tree.clear()
        panel = self.query_one("#findings-panel")
        panel.border_title = _(" NÁLEZY · {len} ").format(len=len(repo.findings))
        first = None
        for cat in CATEGORY_ORDER:
            items = sorted(
                (f for f in repo.findings if f.category is cat),
                key=lambda f: (-f.severity.rank, f.check_id),
            )
            label = Text(f"{cat.label:<20}", style=Style(color=p.text, bold=True))
            label.append(f"{len(items):>3}", style=Style(color=p.muted))
            node = tree.root.add(label, data=None, expand=bool(items))
            for f in items:
                leaf = Text(f"{f.severity.symbol} ", style=Style(color=p.severity(f.severity)))
                leaf.append(f"{f.check_id:<22}", style=Style(color=p.text))
                if f.fixable:
                    leaf.append(" ✓", style=Style(color=p.ok))
                n = node.add_leaf(leaf, data=f)
                if first is None:
                    first = n
        hint = Text(_("✓ opravitelné automaticky\n"), style=Style(color=p.ok))
        hint.append(_("a allowlist · skryje nález"), style=Style(color=p.muted))
        self.query_one("#findings-hint", Static).update(hint)
        self.query_one("#diagnosis").border_title = _(" DIAGNÓZA ")
        if first is not None:
            tree.move_cursor(first)
            self.current = first.data
        else:
            self.current = None
        self._show()

    def _show(self) -> None:
        self.query_one("#diag", Static).update(diagnosis(self.current, self.rd))

    @on(Tree.NodeHighlighted)
    def _node(self, event: Tree.NodeHighlighted[Finding | None]) -> None:
        if event.node.data is not None:
            self.current = event.node.data
            self._show()

    # ------------------------------------------------------------------ akce
    def action_open_fix(self) -> None:
        repo = self.repo
        if repo is None:
            return
        if not any(f.fixable for f in repo.findings):
            self.notify(_("Repo nemá žádné automaticky opravitelné nálezy."), severity="warning")
            return
        self.rd.open_fix(
            repo.path, self.current.check_id if self.current and self.current.fixable else None
        )

    def action_publish_open(self) -> None:
        self.rd.open_publish(self.path)

    def action_allowlist(self) -> None:
        repo, finding = self.repo, self.current
        if repo is None or finding is None:
            self.notify(_("Vyber nález."), severity="warning")
            return
        fp = finding.fingerprint(repo.name)

        def done(reason: str | None) -> None:
            if not reason:
                return
            try:
                config = self.rd.store.add_allow(
                    AllowEntry(hash=fp, reason=reason, check=finding.check_id, repo=repo.name)
                )
            except ConfigError as err:
                self.notify(str(err), severity="error")
                return
            repo.findings = [f for f in repo.findings if f.fingerprint(repo.name) != fp]
            repo.allowlisted += 1
            # MCP server (jiný proces) čte poslední sken z disku, ať vidí i tuhle výjimku
            with contextlib.suppress(OSError):
                history.save_last_scan(self.rd.result)
            self.rd.config_changed(config)
            self.notify(_("{check_id} přidán do allowlistu.").format(check_id=finding.check_id))
            self.render_all()

        self.app.push_screen(
            InputDialog(
                _("Allowlist"),
                _("Proč skrýt nález {check_id}? (uloží se do config.toml)").format(
                    check_id=finding.check_id
                ),
                placeholder=_("např. testovací fixtura, falešný poplach"),
            ),
            done,
        )

    def action_open_editor(self) -> None:
        repo, finding = self.repo, self.current
        if repo is None:
            return
        rel = finding.location.path if finding else None
        target = Path(repo.path) / rel if rel else Path(repo.path)
        if rel and not target.exists():
            self.notify(
                _("Soubor {rel} v pracovním stromu neexistuje (je jen v historii).").format(
                    rel=rel
                ),
                severity="warning",
            )
            return
        cmd = editor_command(
            self.rd.config.ui.editor, target, finding.location.line if finding else None
        )
        try:
            if is_gui_editor(cmd):
                open_in_editor_detached(cmd)
                self.notify(_("Otevírám v {v}: {v2}").format(v=cmd[0], v2=rel or repo.name))
            else:
                with self.app.suspend():
                    subprocess.run(cmd, check=False)
        except FileNotFoundError:
            self.notify(
                _(
                    "Editor {v!r} není nainstalovaný – vyber jiný v Nastavení (klávesa {key})."
                ).format(v=cmd[0], key=self.rd.key("screen_settings")),
                severity="error",
            )
        except (OSError, Exception) as err:
            self.notify(_("Editor nejde spustit: {err}").format(err=err), severity="error")

    def action_open_web(self) -> None:
        repo = self.repo
        if repo is None or not repo.web_url:
            self.notify(_("Repo nemá webovou adresu hostingu."), severity="warning")
            return
        if not open_url(repo.web_url):
            self.notify(
                _("xdg-open není k dispozici: {web_url}").format(web_url=repo.web_url),
                severity="warning",
            )

    def action_export_sbom(self) -> None:
        repo = self.repo
        if repo is None:
            return
        if repo.untrusted_owner:
            self.notify(_("Repo patří jinému uživateli – SBOM nejde vytvořit."), severity="warning")
            return
        default = f"~/sbom/{repo.name}.cdx.json"

        def chosen(value: str | None) -> None:
            if value:
                self.do_export_sbom(Path(repo.path), value)

        self.app.push_screen(
            InputDialog(
                _("Export SBOM"),
                _("CycloneDX 1.5 (JSON) z lockfilů repa – bez sítě. Kam uložit?"),
                value=default,
                confirm_label=_("Exportovat"),
            ),
            chosen,
        )

    @work(thread=True, exclusive=True, group="sbom")
    def do_export_sbom(self, repo_path: Path, target: str) -> None:
        from repo_doctor.paths import expand_path
        from repo_doctor.sbom import write_sbom

        out = expand_path(target)
        try:
            write_sbom(repo_path, out)
        except FileExistsError:
            self.app.call_from_thread(
                self.notify,
                _("{out} už existuje – nepřepisuji.").format(out=out),
                severity="error",
                timeout=8,
            )
            return
        except (OSError, ValueError) as err:
            self.app.call_from_thread(
                self.notify,
                _("SBOM nejde uložit: {err}").format(err=err),
                severity="error",
                timeout=8,
            )
            return
        self.app.call_from_thread(self.notify, _("SBOM uložen: {out}").format(out=out))

    def action_copy_path(self) -> None:
        repo = self.repo
        if repo is None:
            return
        if not wl_copy(repo.path):
            self.app.copy_to_clipboard(repo.path)  # OSC 52
        self.notify(_("Zkopírováno: {path}").format(path=repo.path))
