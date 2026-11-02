"""Aktualizace zranitelných závislostí v lockfilu (oprava `deps-vulnerable`, #71).

repo-doctor lockfily nepřepisuje ručně (hashe, odkazy na soubory a řešení závislostí by se
rozbily). Spustí správce balíčků (`uv`, `npm`) v **dočasné složce** jen s manifestem a lockfilem
z HEAD – repo uživatele se nedotkne, výsledný lockfile se stane obsahem opravné větve.

Bezpečnost: do složky se nekopíruje nic dalšího (žádné `.npmrc`, skripty, zdrojáky), npm běží
s `--ignore-scripts`, uv s `UV_NO_BUILD` (nestaví cizí sdist), názvy balíčků se ověřují regulárním
výrazem (nesmí vypadat jako přepínač) a nástroj má časový limit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from packaging.version import InvalidVersion, Version

from repo_doctor import deps as D
from repo_doctor.gitwrap import Git
from repo_doctor.i18n import _

TIMEOUT_S = 300

# lockfile → (nástroj, manifest ve stejné složce, ekosystém OSV)
SUPPORTED: dict[str, tuple[str, str, D.OsvEcosystem]] = {
    "uv.lock": ("uv", "pyproject.toml", "PyPI"),
    "package-lock.json": ("npm", "package.json", "npm"),
}

_NAME = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._~/-]*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_~-]*$")

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Target:
    """Zranitelná závislost a verze, ve které je opravená."""

    name: str
    version: str  # současná (zranitelná)
    fixed_in: str


@dataclass
class LockUpdate:
    path: str
    old: str
    content: str | None = None  # nový lockfile; None = nic se nezměnilo / selhalo
    upgraded: dict[str, tuple[str, str]] = field(default_factory=dict)  # název → (stará, nová)
    unresolved: list[str] = field(default_factory=list)  # zůstaly na zranitelné verzi
    error: str | None = None


def tool_for(lock_path: str) -> str | None:
    """Cesta ke správci balíčků pro daný lockfile, nebo `None` (nepodporovaný / chybí v PATH)."""
    entry = SUPPORTED.get(PurePosixPath(lock_path).name)
    return shutil.which(entry[0]) if entry else None


def supported(lock_path: str) -> bool:
    return PurePosixPath(lock_path).name in SUPPORTED


def valid_target(name: str, fixed_in: str) -> bool:
    return bool(_NAME.match(name) and _VERSION.match(fixed_in))


def run_tool(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "NODE_OPTIONS"))}
    env.update(
        {
            "UV_NO_BUILD": "1",
            "UV_NO_PROGRESS": "1",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "CI": "1",
        }
    )
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )


def _command(tool: str, lock_path: str, targets: list[Target]) -> list[str]:
    name = PurePosixPath(lock_path).name
    if name == "uv.lock":
        argv = [tool, "lock"]
        for t in targets:
            argv += ["--upgrade-package", f"{t.name}>={t.fixed_in}"]
        return argv
    return [
        tool,
        "update",
        *sorted({t.name for t in targets}),
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]


def _versions(lock_path: str, text: str) -> dict[str, set[str]]:
    name = PurePosixPath(lock_path).name
    _tool, _manifest, eco = SUPPORTED[name]
    deps = (
        D.parse_toml_packages(text, lock_path, eco)
        if name == "uv.lock"
        else D.parse_package_lock(text, lock_path)
    )
    result: dict[str, set[str]] = {}
    for dep in deps:
        result.setdefault(D.norm_name(eco, dep.name), set()).add(dep.version)
    return result


def update_lock(
    git: Git,
    head: str,
    lock_path: str,
    targets: list[Target],
    *,
    runner: Runner | None = None,
) -> LockUpdate:
    """Aktualizuje lockfile na verze s opravou. Pracuje jen v dočasné složce, nic v repu nemění."""
    raw = git.show_file(head, lock_path)
    old = raw.decode("utf-8", "replace") if raw is not None else ""
    update = LockUpdate(path=lock_path, old=old)
    entry = SUPPORTED.get(PurePosixPath(lock_path).name)
    targets = [t for t in targets if valid_target(t.name, t.fixed_in)]
    if raw is None or entry is None or not targets:
        update.error = _("lockfile nejde zpracovat")
        return update
    tool = tool_for(lock_path)
    if tool is None:
        update.error = _("{tool} není v PATH").format(tool=entry[0])
        return update
    parent = PurePosixPath(lock_path).parent
    manifest = git.show_file(head, str(parent / entry[1]))
    if manifest is None:
        update.error = _("vedle lockfilu chybí {manifest}").format(manifest=entry[1])
        return update
    with tempfile.TemporaryDirectory(prefix="repo-doctor-deps-") as tmp:
        workdir = Path(tmp)
        (workdir / PurePosixPath(lock_path).name).write_bytes(raw)
        (workdir / entry[1]).write_bytes(manifest)
        try:
            proc = (runner or run_tool)(_command(tool, lock_path, targets), workdir)
        except (OSError, subprocess.TimeoutExpired) as err:
            update.error = _("{tool} selhal: {err}").format(tool=entry[0], err=err)
            return update
        if proc.returncode != 0:
            tail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
            update.error = _("{tool} skončil chybou: {tail}").format(tool=entry[0], tail=tail)
            return update
        try:
            new = (workdir / PurePosixPath(lock_path).name).read_text("utf-8", "replace")
        except OSError as err:
            update.error = str(err)
            return update
    _evaluate(update, old, new, targets)
    return update


def _evaluate(update: LockUpdate, old: str, new: str, targets: list[Target]) -> None:
    eco = SUPPORTED[PurePosixPath(update.path).name][2]
    after = _versions(update.path, new)
    for t in targets:
        present = after.get(D.norm_name(eco, t.name), set())
        if t.version in present or not present:
            update.unresolved.append(f"{t.name} {t.version}")
            continue
        newest = max(present, key=_sort_key)
        update.upgraded[t.name] = (t.version, newest)
    if new != old and update.upgraded:
        update.content = new


def _sort_key(version: str) -> tuple[int, Version | str]:
    try:
        return (1, Version(version))
    except InvalidVersion:
        return (0, version)
