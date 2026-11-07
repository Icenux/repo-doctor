"""Samostatný HTML report: žádné externí zdroje, filtrování, světlý i tmavý režim."""

from __future__ import annotations

from datetime import datetime
from html import escape

from repo_doctor.czech import repositories
from repo_doctor.i18n import _, language
from repo_doctor.models import Category, RepoResult, ScanResult, Severity
from repo_doctor.reports import local_time, visibility_label
from repo_doctor.scoring import band, gauge, score_label, sort_score

CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--text:#1d2330;--muted:#5b6474;--border:#d5d9e0;--accent:#0f8a6c;
--high:#c8102e;--med:#9a5b00;--low:#1f5fbf;--ok:#1e7b34;--sel:#e8eef8}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#12141a;--panel:#171a21;--text:#c9ced8;
--muted:#8a93a6;--border:#343a47;--accent:#5fd3b0;--high:#ff5f6d;--med:#f5b642;--low:#6fa8ff;--ok:#7bd88f;--sel:#223047}}
:root[data-theme=dark]{--bg:#12141a;--panel:#171a21;--text:#c9ced8;--muted:#8a93a6;--border:#343a47;--accent:#5fd3b0;
--high:#ff5f6d;--med:#f5b642;--low:#6fa8ff;--ok:#7bd88f;--sel:#223047}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header,main{max-width:1200px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px;color:var(--accent)}h2{font-size:15px;margin:0}
.muted{color:var(--muted)}.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;
padding:12px 14px;margin:12px 0}
.vitals{display:flex;flex-wrap:wrap;gap:18px;align-items:center}
.sev-high{color:var(--high)}.sev-medium{color:var(--med)}.sev-low{color:var(--low)}.zero{color:var(--muted);opacity:.6}
.filters{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
select,button,input{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--border);
border-radius:6px;padding:4px 8px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--border);
vertical-align:top}th{color:var(--accent);font-weight:600}
.repo summary{cursor:pointer;list-style:none;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.repo summary::before{content:'▸';color:var(--muted)}.repo[open] summary::before{content:'▾'}
.score{font-weight:700}.s-bad{color:var(--high)}.s-warn{color:var(--med)}.s-ok{color:var(--ok)}
code{background:var(--sel);padding:0 4px;border-radius:4px;overflow-wrap:anywhere}
.stripe{border-left:4px solid var(--border)}.stripe.s-bad{border-color:var(--high)}
.stripe.s-warn{border-color:var(--med)}.stripe.s-ok{border-color:var(--ok)}
.hidden{display:none}
@media (max-width:640px){td:nth-child(4),th:nth-child(4){display:none}}
"""

JS = """
(function(){
  const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));
  function apply(){
    const sev=$$('input[name=sev]:checked').map(e=>e.value);
    const repo=$('#f-repo').value, cat=$('#f-cat').value, q=$('#f-q').value.toLowerCase();
    $$('.repo').forEach(r=>{
      let visible=0;
      r.querySelectorAll('tr.f').forEach(tr=>{
        const ok=sev.includes(tr.dataset.sev)&&(!cat||tr.dataset.cat===cat)&&(!q||tr.textContent.toLowerCase().includes(q));
        tr.classList.toggle('hidden',!ok); if(ok) visible++;
      });
      const repoOk=!repo||r.dataset.repo===repo;
      const empty=r.querySelectorAll('tr.f').length===0;
      const filtering=cat||sev.length<3;
      const nameHit=q&&r.dataset.repo.toLowerCase().includes(q);
      r.classList.toggle('hidden',!repoOk||(visible===0&&!nameHit&&(filtering||q)));
    });
  }
  $$('input[name=sev]').forEach(e=>e.addEventListener('change',apply));
  ['#f-repo','#f-cat'].forEach(s=>$(s).addEventListener('change',apply));
  $('#f-q').addEventListener('input',apply);
  $('#theme').addEventListener('click',()=>{
    const root=document.documentElement;
    const dark=root.dataset.theme?root.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme=dark?'light':'dark';
  });
})();
"""


def _score_class(score: int | None) -> str:
    if score is None:
        return "muted"
    return "s-bad" if score < 40 else "s-warn" if score < 70 else "s-ok"


def _count_span(sev: Severity, n: int) -> str:
    cls = f"sev-{sev.value}" if n else "zero"
    return f'<span class="{cls}" title="{sev.short}">{sev.symbol} {n}</span>'


def _repo(r: RepoResult) -> str:
    counts = r.counts()
    cls = _score_class(r.score)
    vis = visibility_label(r.visibility)
    head = (
        f'<summary><span class="score {cls}">{score_label(r.score)} {gauge(r.score)}</span>'
        f"<h2>{escape(r.name)}</h2><span class=muted>{escape(band(r).label)}</span>"
        + "".join(_count_span(s, counts[s]) for s in Severity)
        + f"<span class=muted>{escape(r.forge or '')} {escape(vis)}</span></summary>"
    )
    rows = []
    for f in sorted(r.findings, key=lambda f: (-f.severity.rank, f.check_id)):
        where = escape(f.location.render())
        if f.snippet:
            where += f" <code>{escape(f.snippet)}</code>"
        fix = " ✓" if f.fixable else ""
        rows.append(
            f'<tr class=f data-sev="{f.severity.value}" data-cat="{f.category.value}">'
            f'<td class="sev-{f.severity.value}">{f.severity.symbol} {f.severity.short}</td>'
            f"<td><code>{escape(f.check_id)}</code>{fix}</td><td>{escape(f.message)}</td><td>{where}</td></tr>"
        )
    body = (
        "<table><thead><tr>"
        + _("<th>severity</th><th>kontrola</th><th>nález</th><th>kde</th>")
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        if rows
        else "<p class=muted>" + _("Bez nálezů ✓") + "</p>"
    )
    extra = ""
    if r.skipped:
        extra += (
            "<p class=muted>"
            + _("Přeskočeno: ")
            + escape(", ".join(f"{k} ({v})" for k, v in sorted(r.skipped.items())))
            + "</p>"
        )
    if r.errors:
        extra += (
            "<p class=sev-high>"
            + _("Chyby: ")
            + escape(", ".join(f"{k}: {v}" for k, v in sorted(r.errors.items())))
            + "</p>"
        )
    return (
        f'<details class="panel repo stripe {cls}" data-repo="{escape(r.name)}" {"open" if r.findings else ""}>'
        f"{head}<p class=muted>{escape(r.path)}</p>{body}{extra}</details>"
    )


def render(result: ScanResult, *, no_pulse_days: int = 90, now: datetime | None = None) -> str:
    s = result.summary(no_pulse_days, now)
    when = local_time(result.finished_at or result.started_at)
    repos = sorted(result.repos, key=lambda r: (sort_score(r.score), r.name))
    repo_options = "".join(
        f'<option value="{escape(r.name)}">{escape(r.name)}</option>' for r in repos
    )
    cat_options = "".join(f'<option value="{c.value}">{escape(c.label)}</option>' for c in Category)
    sev_boxes = "".join(
        f'<label class="sev-{sv.value}"><input type=checkbox name=sev value="{sv.value}" checked> {sv.symbol} {sv.short}</label>'
        for sv in Severity
    )
    warnings = "".join(f"<li>{escape(w)}</li>" for w in result.warnings)
    return f"""<!doctype html>
<html lang="{language()}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>repo-doctor report</title>
<style>{CSS}</style>
</head>
<body>
<header>
<h1>repo-doctor · report</h1>
<div class="panel vitals">
<span>{repositories(s.repos)}</span>
{_count_span(Severity.HIGH, s.by_severity[Severity.HIGH])}
{_count_span(Severity.MEDIUM, s.by_severity[Severity.MEDIUM])}
{_count_span(Severity.LOW, s.by_severity[Severity.LOW])}
<span>{escape(_("zdraví"))} <b class="{_score_class(s.health)}">{s.health}</b></span>
<span class=muted>{escape(_("sken"))} {escape(when)}{escape(_(" · offline")) if result.offline else ""}{escape(_(" · přerušený")) if result.cancelled else ""}</span>
<button id=theme type=button title="{escape(_("Přepnout světlý/tmavý režim"))}">{escape(_("◐ téma"))}</button>
</div>
{f'<div class="panel"><b>{escape(_("Upozornění"))}</b><ul>{warnings}</ul></div>' if warnings else ""}
<div class="panel filters">
{sev_boxes}
<label>{escape(_("repo"))} <select id=f-repo><option value="">{escape(_("všechna"))}</option>{repo_options}</select></label>
<label>{escape(_("kategorie"))} <select id=f-cat><option value="">{escape(_("všechny"))}</option>{cat_options}</select></label>
<label>{escape(_("hledat"))} <input id=f-q type=search placeholder="{escape(_("text nálezu"))}"></label>
</div>
</header>
<main>
{"".join(_repo(r) for r in repos)}
</main>
<script>{JS}</script>
</body>
</html>
"""
