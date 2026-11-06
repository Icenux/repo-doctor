from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.text import Text

from repo_doctor import details
from repo_doctor.deps import Dependency, DepsData
from repo_doctor.forges import gitea, github, gitlab
from repo_doctor.gitwrap import Git, TreeEntry
from repo_doctor.tui.screens.detail import LABEL_W, detail_items, layout_items
from repo_doctor.tui.theme import PALETTE as p
from tests.factory import RepoBuilder
from tests.sample import sample_result
from tests.tui_helpers import make_app, screen_text

T0 = 1_700_000_000  # 14. 11. 2023


def test_language_of_and_exclusions() -> None:
    assert details.language_of("src/app.py") == "Python"
    assert details.language_of("web/App.TSX") == "TypeScript"
    assert details.language_of("deploy/Dockerfile") == "Dockerfile"
    assert details.language_of("README.md") is None  # prostý text se nepočítá
    assert details.language_of("data.json") is None
    assert details.language_of("node_modules/x/index.js") is None
    assert details.language_of("static/app.min.js") is None


def test_languages_sorted_by_size() -> None:
    entries = [
        TreeEntry("a.py", 300, "1"),
        TreeEntry("b.py", 100, "2"),
        TreeEntry("run.sh", 500, "3"),
        TreeEntry("empty.go", 0, "4"),
        TreeEntry("notes.md", 9999, "5"),
    ]
    assert [(lang.name, lang.bytes) for lang in details.languages(entries)] == [
        ("Shell", 500),
        ("Python", 400),
    ]


def test_ecosystems_managers_and_counts() -> None:
    files = ["uv.lock", "pyproject.toml", "web/package.json", "web/pnpm-lock.yaml", "go.mod"]
    deps = DepsData(
        dependencies=[
            Dependency("PyPI", "httpx", "1", "uv.lock", direct=True),
            Dependency("PyPI", "anyio", "1", "uv.lock"),
            Dependency("PyPI", "anyio", "1", "uv.lock"),  # duplicita se počítá jednou
            Dependency("npm", "react", "18", "web/pnpm-lock.yaml", direct=True),
        ]
    )
    result = {e.name: e for e in details.ecosystems(files, deps)}
    assert result["python"].managers == ["uv"]
    assert (result["python"].direct, result["python"].total) == (1, 2)
    assert result["node"].managers == ["pnpm"] and result["node"].total == 1
    assert result["go"].managers == ["go"] and result["go"].total == 0
    assert details.ecosystems(["vendor/x/go.mod"], None) == []


def test_collect_from_git(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "r")
    rb.write("app.py", "print(1)\n" * 10).write("run.sh", "echo\n").commit("a", ts=T0)
    rb.git("tag", "v1.0.0")
    rb.write("README.md", "x\n").commit("b", ts=T0 + 86400)
    rb.branch("dev")
    now = datetime.fromtimestamp(T0, UTC) + timedelta(days=10)
    files = Git(rb.path).ls_files()
    d = details.collect(Git(rb.path), files=files, deps=None, license="MIT", now=now)
    assert [lang.name for lang in d.languages] == ["Python", "Shell"]
    assert d.files == 3 and d.worktree_bytes and d.git_bytes
    assert d.first_commit == datetime.fromtimestamp(T0, UTC)
    assert d.commits_30d == 2 and d.commits_year == 2
    assert d.authors == 1 and d.top_authors[0].commits == 2
    assert d.branches == 2 and d.license == "MIT"
    assert d.last_tag == "v1.0.0" and d.last_tag_date == datetime.fromtimestamp(T0, UTC)


def test_collect_empty_repo(tmp_path: Path) -> None:
    rb = RepoBuilder.create(tmp_path / "e")
    d = details.collect(Git(rb.path), files=[], deps=None, license=None, now=datetime.now(UTC))
    assert d.languages == [] and d.first_commit is None and d.last_tag is None
    assert d.authors == 0 and d.top_authors == []


def test_forge_repo_fields() -> None:
    gh = github._repo(
        {
            "full_name": "o/r",
            "stargazers_count": 5,
            "forks_count": 2,
            "open_issues_count": 7,
            "topics": ["cli", 3],
            "created_at": "2024-01-02T00:00:00Z",
        }
    )
    assert (gh.stars, gh.forks, gh.open_issues, gh.topics) == (5, 2, 7, ("cli",))
    assert gh.created_at == datetime(2024, 1, 2, tzinfo=UTC)
    gt = gitea._repo({"full_name": "o/r", "stars_count": 1, "forks_count": 0, "topics": None})
    assert (gt.stars, gt.forks, gt.open_issues, gt.topics) == (1, 0, None, ())
    gl = gitlab._repo({"path_with_namespace": "g/p", "star_count": 4, "tag_list": ["a"]})
    assert gl.stars == 4 and gl.topics == ("a",)
    assert github._repo({"full_name": "o/r", "stargazers_count": True}).stars is None


def test_layout_wraps_or_truncates() -> None:
    items = [Text(f"položka-{i}") for i in range(6)]
    wrapped = layout_items("HISTORIE", items, 40, wrap=True, p=p)
    assert len(wrapped) > 1 and all(line.cell_len <= 40 for line in wrapped)
    assert wrapped[1].plain.startswith(" " * LABEL_W)
    assert "položka-5" in "".join(line.plain for line in wrapped)
    cut = layout_items("HISTORIE", items, 40, wrap=False, p=p)
    assert len(cut) == 1 and cut[0].plain.endswith("…")


async def test_card_shows_details(tmp_path: Path) -> None:
    result = sample_result()
    infra = result.repos[0]
    app = make_app(tmp_path, result=result)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.open_detail(infra.path)
        await pilot.pause()
        text = screen_text(app)
        for needle in ("KÓD", "HCL", "64 %", "python (uv)", "HISTORIE", "tag v2.3.0", "★ 3"):
            assert needle in text, needle
        labels = [label for label, _ in detail_items(infra, app)]
        assert labels == ["KÓD", "ZÁVISLOSTI", "HISTORIE", "NA HOSTINGU"]
        bare = result.repos[1]  # bez detailů ze skenu zbude jen počet commitů ze stavu
        assert [label for label, _ in detail_items(bare, app)] == ["HISTORIE"]
