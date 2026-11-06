"""Ochrana rozhodnutí „repo-doctor se neváže na uv“ (#86).

Aplikaci hlídá `test_no_external_tools.py`. Tady se hlídá vývoj a vydání: zámky jsou
`requirements-*.txt` s hashi, CI a sestavení používají `pip`, šablona CI pro Python nevyžaduje `uv`.
Rozpoznávání a přepis `uv.lock` v cizích projektech zůstává (funkce produktu, ne závislost).
"""

from __future__ import annotations

import re
from pathlib import Path

from repo_doctor import templates

ROOT = Path(__file__).parent.parent
UV_WORD = re.compile(r"(?<![\w.-])uvx?(?![\w.-])")


def _dev_files() -> list[Path]:
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    files += sorted((ROOT / "packaging").rglob("*.sh"))
    files.append(ROOT / "pyproject.toml")
    return files


def test_no_uv_lockfile_in_repo() -> None:
    assert not (ROOT / "uv.lock").exists()
    assert not (ROOT / "src" / "repo_doctor" / "uv.lock").exists()


def test_ci_and_build_do_not_use_uv() -> None:
    offenders = [
        f"{f.relative_to(ROOT)}:{n}: {line.strip()}"
        for f in _dev_files()
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), start=1)
        if UV_WORD.search(line) or "astral-sh" in line
    ]
    assert offenders == []


def test_python_ci_template_does_not_need_uv() -> None:
    template = templates.CI["python"]
    assert UV_WORD.search(template) is None
    assert "astral-sh" not in template
    assert "pip install" in template


def test_lockfiles_are_hashed_and_cover_extras() -> None:
    for name, marker in (
        ("requirements-dev.txt", "pytest=="),
        ("requirements-build.txt", "pyinstaller=="),
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert marker in text, name
        assert "textual==" in text, name  # běhové závislosti jsou v obou
        # každý pin má hash, jinak by `pip install --require-hashes` selhal
        lines = text.splitlines()
        pins = [i for i, line in enumerate(lines) if re.match(r"[A-Za-z0-9_.\-]+==", line)]
        assert pins, name
        assert all("--hash=sha256:" in lines[i + 1] for i in pins), name
