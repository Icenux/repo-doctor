"""Průběh CI jako graf jobů: závislosti (`needs`) z workflow a rozložení do sloupců.

Čistá logika bez UI a sítě. Hosting dodá joby (`CIJob`); GitHub závislosti nevrací, proto se čtou
z workflow souboru na commitu PR (`attach_needs`, jen `yaml.safe_load`, nic se nespouští).
`layout` rozdělí joby po workflow/pipeline a každou skupinu do sloupců:

- jsou-li závislosti: sloupec = nejdelší cesta, zdroje se posunou co nejblíž svým následníkům
  (kratší spojnice) a nadbytečné hrany (přímá, i když vede cesta přes jiné joby) se vynechají;
- jinak podle stage (GitLab), a bez obojího je jediný sloupec.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import yaml

from repo_doctor.forges.base import CIJob

MAX_WORKFLOW_BYTES = 256 * 1024


@dataclass
class Graph:
    """Jedna skupina jobů (workflow / pipeline) připravená k vykreslení."""

    title: str
    jobs: list[CIJob]
    columns: list[list[int]] = field(default_factory=list)  # indexy do `jobs`
    column_titles: list[str] = field(default_factory=list)  # např. názvy stages; jinak prázdné
    edges: list[tuple[int, int]] = field(default_factory=list)  # zdroj → cíl, sousední sloupce
    long_edges: list[tuple[int, int]] = field(default_factory=list)  # přes víc sloupců, nekreslí se

    def column_of(self, index: int) -> int:
        for i, col in enumerate(self.columns):
            if index in col:
                return i
        raise ValueError(index)


# ---------------------------------------------------------------------------- závislosti z YAML


def parse_needs(text: str) -> dict[str, tuple[str, list[str]]]:
    """`{id jobu: (zobrazený název, [id jobů, na kterých závisí])}` z workflow YAML."""
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError, ValueError, TypeError, MemoryError):
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return {}
    out: dict[str, tuple[str, list[str]]] = {}
    for job_id, body in jobs.items():
        if not isinstance(body, dict):
            continue
        key = str(job_id)
        name = body.get("name")
        needs = body.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        out[key] = (
            name if isinstance(name, str) else key,
            [str(n) for n in needs if isinstance(n, str)] if isinstance(needs, list) else [],
        )
    return out


def _belongs(api_name: str, label: str) -> bool:
    """Patří job z API (`test (3.12)`, `caller / inner`) k jobu `label` z YAML?"""
    return api_name == label or api_name.startswith((f"{label} (", f"{label} / "))


def attach_needs(jobs: list[CIJob], read: Callable[[str], str | None]) -> None:
    """Doplní `needs` jobům podle jejich workflow souboru (`read(cesta)` vrací text nebo None)."""
    for path in dict.fromkeys(j.workflow_path for j in jobs if j.workflow_path):
        text = read(path)
        if not text or len(text) > MAX_WORKFLOW_BYTES:
            continue
        spec = parse_needs(text)
        mine = [j for j in jobs if j.workflow_path == path]
        owner: dict[str, str] = {}  # název jobu z API → id v YAML (nejdelší shoda vyhrává)
        for job in mine:
            best = ""
            for job_id, (name, _needs) in spec.items():
                for label in (name, job_id):
                    if _belongs(job.name, label) and len(label) > len(best):
                        best, owner[job.name] = label, job_id
        for job in mine:
            for need_id in spec.get(owner.get(job.name, ""), ("", []))[1]:
                job.needs += [m.name for m in mine if owner.get(m.name) == need_id]


# ---------------------------------------------------------------------------- rozložení


def _levels(n: int, preds: list[set[int]]) -> list[int]:
    level = [0] * n
    state = [0] * n  # 0 nenavštíveno, 1 rozpracováno (cyklus se ignoruje), 2 hotovo

    def visit(i: int) -> int:
        if state[i] == 2:
            return level[i]
        if state[i] == 1:
            return 0
        state[i] = 1
        level[i] = 1 + max((visit(p) for p in preds[i]), default=-1)
        state[i] = 2
        return level[i]

    for i in range(n):
        visit(i)
    return level


def _reduce(n: int, preds: list[set[int]]) -> list[set[int]]:
    """Odstraní hrany, které už vyplývají z cesty přes jiné joby (tranzitivní redukce)."""
    ancestors: list[set[int] | None] = [None] * n

    def anc(i: int, seen: frozenset[int] = frozenset()) -> set[int]:
        cached = ancestors[i]
        if cached is not None:
            return cached
        out: set[int] = set()
        for p in preds[i]:
            if p in seen or p == i:
                continue
            out |= {p} | anc(p, seen | {i})
        ancestors[i] = out
        return out

    return [
        {p for p in preds[i] if not any(p in anc(q) for q in preds[i] if q != p)} for i in range(n)
    ]


def build(jobs: list[CIJob], title: str = "") -> Graph:
    """Sloupce a hrany jedné skupiny jobů."""
    n = len(jobs)
    index = {j.name: i for i, j in enumerate(jobs)}
    preds = [{index[d] for d in j.needs if d in index and d != j.name} for j in jobs]
    graph = Graph(title, jobs)
    if any(preds):
        level = _levels(n, preds)
        succs: list[set[int]] = [set() for _ in range(n)]
        for d, ps in enumerate(preds):
            for p in ps:
                succs[p].add(d)
        for i in sorted(range(n), key=lambda k: -level[k]):  # jen směrem doprava, k následníkům
            if succs[i]:
                level[i] = max(level[i], min(level[s] for s in succs[i]) - 1)
        used = sorted(set(level))
        col = [used.index(lv) for lv in level]
        reduced = _reduce(n, preds)
        for d in range(n):
            for p in sorted(reduced[d]):
                (graph.edges if col[d] - col[p] == 1 else graph.long_edges).append((p, d))
    elif any(j.stage for j in jobs):
        stages = list(dict.fromkeys(j.stage for j in jobs))
        col = [stages.index(j.stage) for j in jobs]
        graph.column_titles = stages
    else:
        col = [0] * n
    graph.columns = [[i for i in range(n) if col[i] == c] for c in range(max(col, default=-1) + 1)]
    if not graph.column_titles:
        graph.column_titles = [""] * len(graph.columns)
    return graph


def layout(jobs: list[CIJob]) -> list[Graph]:
    """Graf pro každý workflow / pipeline zvlášť, v pořadí prvního výskytu."""
    groups: dict[str, list[CIJob]] = {}
    for job in jobs:
        groups.setdefault(job.workflow, []).append(job)
    return [build(group, title) for title, group in groups.items()]
