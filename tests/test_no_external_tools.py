"""Ochrana principu „repo-doctor funguje hned po instalaci“ (#71).

Aplikace se instaluje z balíčku a po instalaci musí fungovat bez dalších programů – stačí `git`.
Nic, co by uživatel musel doinstalovat (`uv`, `npm`, `pip`, `cargo`, `go`…), se nesmí stát
podmínkou funkce, a repo-doctor je nesmí spouštět místo uživatele. Volitelné programy (`gitleaks`,
`notify-send`, `xdg-open`, `wl-copy`, `gpg`, `systemctl`, `sendmail`) mají záložní chování
a zapisují se sem výslovně. Přidáváš-li další, zapiš ho do `OPTIONAL_PROGRAMS` s důvodem
a ujisti se, že bez něj aplikace funguje dál (jen s vypnutou funkcí).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "repo_doctor"

# moduly, které smějí spouštět programy (git je vždy povinný, ostatní jsou volitelné)
PROCESS_MODULES = {
    "gitwrap.py",  # git – jediný povinný program
    "tools.py",  # volitelné nástroje: instalace přes správce balíčků distribuce
    "update.py",  # ověření podpisu aktualizace (gpg, volitelně)
    "notify.py",  # notify-send, sendmail
    "schedule.py",  # systemctl --user
    "forges/tokens.py",  # pomocník pro tokeny hostingu
    "tui/util.py",  # xdg-open, wl-copy, editor
    "tui/screens/detail.py",  # otevření v editoru
    "checks/secrets.py",  # gitleaks (volitelný, záloha je vlastní skener)
}
OPTIONAL_PROGRAMS = {
    "gpg",
    "gpgv",
    "notify-send",
    "sendmail",
    "xdg-open",
    "wl-copy",
    "systemctl",
    "repo-doctor",
    "sudo",
    "gitleaks",
}
# správci balíčků a jazykové nástroje, které se nesmí spouštět ani vyžadovat
FORBIDDEN = {
    "uv",
    "uvx",
    "pip",
    "pip3",
    "pipx",
    "poetry",
    "pdm",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "bun",
    "node",
    "cargo",
    "rustc",
    "go",
    "python",
    "python3",
}


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _calls_which(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_which = (isinstance(func, ast.Attribute) and func.attr == "which") or (
                isinstance(func, ast.Name) and func.id == "which"
            )
            arg = node.args[0]
            if is_which and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.append(arg.value)
    return names


def test_only_known_modules_start_processes() -> None:
    offenders = []
    for path in _sources():
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if "subprocess" in imports and rel not in PROCESS_MODULES:
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} spouští programy. repo-doctor musí fungovat po instalaci bez dalších nástrojů "
        "(stačí git) – řešení napiš v Pythonu, viz repo_doctor/lockfiles. Je-li program opravdu "
        "volitelný, přidej modul do PROCESS_MODULES v tests/test_no_external_tools.py."
    )


def test_no_package_manager_is_looked_up_or_run() -> None:
    offenders = []
    for path in _sources():
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        for name in _calls_which(tree):
            if name in FORBIDDEN or name not in OPTIONAL_PROGRAMS:
                offenders.append(f"{rel}: which({name!r})")
        if rel in PROCESS_MODULES:
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                    first = node.elts[0]
                    if isinstance(first, ast.Constant) and first.value in FORBIDDEN:
                        offenders.append(f"{rel}:{node.lineno}: příkaz {first.value!r}")
    assert not offenders, offenders


def test_lockfile_fix_needs_no_program() -> None:
    """Oprava lockfilů je čistý Python: balíček `lockfiles` ani kontrola závislostí nic nespouští."""
    for rel in [
        *(p.relative_to(SRC).as_posix() for p in (SRC / "lockfiles").glob("*.py")),
        "checks/deps.py",
    ]:
        text = (SRC / rel).read_text("utf-8")
        assert "subprocess" not in text and "os.system" not in text, rel
