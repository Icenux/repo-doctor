"""Aktualizace zranitelných závislostí v lockfilech (oprava `deps-vulnerable`, #71).

repo-doctor funguje hned po instalaci, bez dalších nástrojů: lockfile přepíše sám, čistě v Pythonu,
a verze, hashe a odkazy bere z metadat registru (PyPI, npm). Žádný `uv`, `npm` ani jiný program
se nespouští (hlídá `tests/test_no_external_tools.py`).

Přepis je záměrně opatrný: změní jen verzi jedné závislosti a odmítne všechno, kde by bylo nutné
znovu řešit strom závislostí (jiné závislosti nové verze, omezení dalších balíčků, neobvyklý
formát, soukromý registr…). Odmítnutí nese důvod pro uživatele. Formát výstupu se u `uv.lock`
ověřuje proti staré verzi téhož balíčku, takže se nikdy nezapíše něco, co neumíme zreprodukovat.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol

from repo_doctor.gitwrap import Git
from repo_doctor.httpclient import HttpClient, HttpError
from repo_doctor.i18n import _

PYPI_URL = "https://pypi.org/pypi/{name}/{version}/json"
NPM_URL = "https://registry.npmjs.org/{name}/{version}"

# lockfile → ekosystém
SUPPORTED: dict[str, str] = {"uv.lock": "PyPI", "package-lock.json": "npm"}

_NAME = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._~/-]*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_~-]*$")


class Refused(Exception):
    """Lockfile nejde bezpečně přepsat; zpráva je důvod pro uživatele."""


class RegistryError(Exception):
    """Registr neodpověděl (síť, neexistující verze)."""


class Registry(Protocol):
    def pypi_version(self, name: str, version: str) -> dict[str, Any]: ...

    def npm_version(self, name: str, version: str) -> dict[str, Any]: ...


class HttpRegistry:
    """Metadata verzí z veřejných registrů (sdílí cache a síťová pravidla s ostatními kontrolami)."""

    def __init__(self, ttl: float = 900) -> None:
        self.ttl = ttl

    def _get(self, url: str) -> dict[str, Any]:
        async def go() -> Any:
            async with HttpClient(namespace="public", ttl=self.ttl) as client:
                return await client.get_json(url)

        try:
            body = asyncio.run(go())
        except HttpError as err:
            raise RegistryError(str(err)) from err
        if not isinstance(body, dict):
            raise RegistryError(_("neočekávaná odpověď registru"))
        return body

    def pypi_version(self, name: str, version: str) -> dict[str, Any]:
        return self._get(PYPI_URL.format(name=name, version=version))

    def npm_version(self, name: str, version: str) -> dict[str, Any]:
        return self._get(NPM_URL.format(name=name.replace("/", "%2F"), version=version))


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
    unresolved: list[tuple[str, str]] = field(default_factory=list)  # (závislost, důvod)
    error: str | None = None


def supported(lock_path: str) -> bool:
    return PurePosixPath(lock_path).name in SUPPORTED


def valid_target(name: str, fixed_in: str) -> bool:
    return bool(_NAME.match(name) and _VERSION.match(fixed_in))


def update_lock(
    git: Git,
    head: str,
    lock_path: str,
    targets: list[Target],
    *,
    registry: Registry | None = None,
) -> LockUpdate:
    """Aktualizuje lockfile na verze s opravou. Čte jen z gitu a z registru, nic v repu nemění."""
    from repo_doctor.lockfiles import npm, pypi

    raw = git.show_file(head, lock_path)
    old = raw.decode("utf-8", "replace") if raw is not None else ""
    update = LockUpdate(path=lock_path, old=old)
    eco = SUPPORTED.get(PurePosixPath(lock_path).name)
    if raw is None or eco is None:
        update.error = _("lockfile nejde zpracovat")
        return update
    registry = registry or HttpRegistry()
    text = old
    for t in targets:
        label = f"{t.name} {t.version}"
        if not valid_target(t.name, t.fixed_in):
            update.unresolved.append((label, _("neplatný název nebo verze")))
            continue
        try:
            text, new = (npm.update if eco == "npm" else pypi.update)(text, t, registry)
        except Refused as why:
            update.unresolved.append((label, str(why)))
        except RegistryError as err:
            update.unresolved.append((label, _("registr neodpověděl: {err}").format(err=err)))
        else:
            update.upgraded[t.name] = (t.version, new)
    if update.upgraded and text != old:
        update.content = text
    return update
