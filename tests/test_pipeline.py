"""Graf průběhu CI: závislosti z workflow YAML a rozložení jobů do sloupců."""

from __future__ import annotations

from repo_doctor.forges.base import CIJob
from repo_doctor.pipeline import Graph, attach_needs, build, layout, parse_needs

WORKFLOW = """
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
  test:
    name: Testy
    needs: lint
    strategy:
      matrix:
        py: ["3.12", "3.13"]
  build:
    needs: [lint, test]
  deploy:
    needs: [build]
  docs:
    uses: ./.github/workflows/docs.yml
"""


def job(name: str, *needs: str, **kw: str) -> CIJob:
    return CIJob(name, "success", needs=list(needs), **kw)  # type: ignore[arg-type]


def names(g: Graph) -> list[list[str]]:
    return [[g.jobs[i].name for i in col] for col in g.columns]


def test_parse_needs_reads_names_and_dependencies() -> None:
    spec = parse_needs(WORKFLOW)
    assert spec["lint"] == ("lint", [])
    assert spec["test"] == ("Testy", ["lint"])
    assert spec["build"] == ("build", ["lint", "test"])
    assert (
        parse_needs("jobs: [1, 2]") == {}
        and parse_needs(": : :\n\t") == {}
        and parse_needs("") == {}
    )


def test_attach_needs_matches_matrix_and_reusable_jobs() -> None:
    jobs = [
        job("lint", workflow_path="ci.yml"),
        job("Testy (3.12)", workflow_path="ci.yml"),
        job("Testy (3.13)", workflow_path="ci.yml"),
        job("build", workflow_path="ci.yml"),
        job("deploy", workflow_path="ci.yml"),
        job("docs / render", workflow_path="ci.yml"),
        job("jiný", workflow_path="neexistuje.yml"),
    ]
    attach_needs(jobs, lambda path: WORKFLOW if path == "ci.yml" else None)
    by = {j.name: j.needs for j in jobs}
    assert by["lint"] == [] and by["Testy (3.12)"] == ["lint"]
    assert by["build"] == ["lint", "Testy (3.12)", "Testy (3.13)"]
    assert by["deploy"] == ["build"] and by["docs / render"] == [] and by["jiný"] == []


def test_attach_needs_ignores_oversized_or_broken_files() -> None:
    jobs = [job("a", workflow_path="x.yml")]
    attach_needs(jobs, lambda _p: "x" * 300_000)
    attach_needs(jobs, lambda _p: "jobs: {a: {needs: ??")
    assert jobs[0].needs == []


def test_columns_follow_the_longest_path_and_drop_redundant_edges() -> None:
    g = build(
        [job("lint"), job("test", "lint"), job("build", "lint", "test"), job("deploy", "build")]
    )
    assert names(g) == [["lint"], ["test"], ["build"], ["deploy"]]
    assert [(g.jobs[s].name, g.jobs[d].name) for s, d in g.edges] == [
        ("lint", "test"),
        ("test", "build"),
        ("build", "deploy"),
    ]  # lint → build vyplývá z cesty přes test
    assert g.long_edges == []


def test_independent_job_moves_next_to_its_successor() -> None:
    g = build([job("a"), job("b", "a"), job("x"), job("c", "b", "x")])
    assert names(g) == [["a"], ["b", "x"], ["c"]]  # x není ve sloupci 0, ale hned před c
    assert g.long_edges == []


def test_edge_over_several_columns_is_kept_as_long_edge() -> None:
    g = build([job("a"), job("b", "a"), job("z"), job("c", "z"), job("d", "a", "c")])
    assert names(g) == [["a", "z"], ["b", "c"], ["d"]]
    assert [(g.jobs[s].name, g.jobs[d].name) for s, d in g.long_edges] == [("a", "d")]
    assert all(g.column_of(d) - g.column_of(s) == 1 for s, d in g.edges)


def test_stages_become_columns_without_dependencies() -> None:
    g = build([job("lint", stage="check"), job("unit", stage="test"), job("e2e", stage="test")])
    assert names(g) == [["lint"], ["unit", "e2e"]] and g.column_titles == ["check", "test"]
    assert g.edges == []


def test_no_structure_is_a_single_column_and_cycles_do_not_hang() -> None:
    assert names(build([job("a"), job("b")])) == [["a", "b"]]
    cyc = build([job("a", "b"), job("b", "a")])
    assert sum(map(len, cyc.columns)) == 2


def test_layout_groups_by_workflow() -> None:
    graphs = layout([job("a", workflow="CI"), job("b", workflow="Docs"), job("c", workflow="CI")])
    assert [(g.title, [j.name for j in g.jobs]) for g in graphs] == [
        ("CI", ["a", "c"]),
        ("Docs", ["b"]),
    ]


def test_parse_needs_survives_hostile_yaml() -> None:
    assert parse_needs("a: &a [*a]\n") == {}  # odkaz sám na sebe
    assert parse_needs("jobs:\n  x:\n    needs: {1: 2}\n") == {"x": ("x", [])}
    deep = "[" * 50_000 + "]" * 50_000  # hluboké vnoření nesmí shodit skenování
    assert parse_needs(deep) == {}
