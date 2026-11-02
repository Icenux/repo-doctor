"""`package-lock.json` (lockfileVersion 3): přepis verze jednoho balíčku podle metadat npm registru."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from repo_doctor.i18n import _
from repo_doctor.lockfiles import Refused, semver

if TYPE_CHECKING:
    from repo_doctor.lockfiles import Registry, Target

REGISTRY_PREFIX = "https://registry.npmjs.org/"
_DEP_KEYS = ("dependencies", "optionalDependencies", "peerDependencies")


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def resolve(packages: dict[str, Any], origin: str, name: str) -> str | None:
    """Kam Node dohledá `name` pro balíček na cestě `origin` (nejbližší `node_modules` směrem nahoru)."""
    current = origin
    while True:
        candidate = f"{current}/node_modules/{name}" if current else f"node_modules/{name}"
        if candidate in packages:
            return candidate
        if not current:
            return None
        cut = current.rfind("/node_modules/")
        current = current[:cut] if cut >= 0 else ""


def _is_project(path: str) -> bool:
    """Kořen nebo workspace (ne nainstalovaný balíček) – tam se počítají i `devDependencies`."""
    return not path.startswith("node_modules/") and "/node_modules/" not in path


def update(text: str, t: Target, registry: Registry) -> tuple[str, str]:
    try:
        data = json.loads(text)
    except ValueError as err:
        raise Refused(_("lockfile není platný JSON")) from err
    if not isinstance(data, dict) or _dump(data) != text:
        raise Refused(_("lockfile má neobvyklé formátování – přepsal by se celý"))
    packages = data.get("packages")
    if data.get("lockfileVersion") != 3 or not isinstance(packages, dict):
        raise Refused(_("podporovaný je jen package-lock.json s lockfileVersion 3"))
    new, old = semver.parse_release(t.fixed_in), semver.parse_release(t.version)
    if new is None or old is None or new <= old:
        raise Refused(
            _("opravená verze {version} není vyšší běžná verze").format(version=t.fixed_in)
        )
    suffix = f"node_modules/{t.name}"
    paths = [
        p
        for p, e in packages.items()
        if (p == suffix or p.endswith("/" + suffix))
        and isinstance(e, dict)
        and e.get("version") == t.version
    ]
    if not paths:
        raise Refused(_("{name} {version} v lockfilu není").format(name=t.name, version=t.version))
    meta = registry.npm_version(t.name, t.fixed_in)
    raw_dist = meta.get("dist")
    dist: dict[str, Any] = raw_dist if isinstance(raw_dist, dict) else {}
    tarball, integrity = str(dist.get("tarball", "")), str(dist.get("integrity", ""))
    if meta.get("version") != t.fixed_in or meta.get("name") != t.name:
        raise Refused(_("registr vrátil jinou verzi, než je opravená"))
    if meta.get("deprecated"):
        raise Refused(_("opravená verze je v registru označená jako zastaralá"))
    if not tarball.startswith(REGISTRY_PREFIX) or not integrity.startswith(("sha512-", "sha384-")):
        raise Refused(_("registr neposkytl ověřitelný odkaz a kontrolní součet"))
    for p in paths:
        _check_entry(packages[p], meta, t)
    _check_dependents(packages, paths, t)
    for p in paths:
        entry = packages[p]
        entry["version"] = t.fixed_in
        entry["resolved"] = tarball
        entry["integrity"] = integrity
        if isinstance(meta.get("license"), str) and "license" in entry:
            entry["license"] = meta["license"]
    return _dump(data), t.fixed_in


def _check_entry(entry: dict[str, Any], meta: dict[str, Any], t: Target) -> None:
    if "name" in entry or entry.get("link"):
        raise Refused(_("balíček je alias nebo odkaz (workspace)"))
    if not str(entry.get("resolved", "")).startswith(REGISTRY_PREFIX):
        raise Refused(_("balíček nepochází z registru npmjs.org (git, soukromý registr, soubor)"))
    for key in _DEP_KEYS:
        if (entry.get(key) or {}) != (meta.get(key) or {}):
            raise Refused(
                _("nová verze má jiné závislosti ({key}) – strom by bylo nutné přepočítat").format(
                    key=key
                )
            )
    if bool(entry.get("hasInstallScript")) != bool(meta.get("hasInstallScript")):
        raise Refused(_("nová verze mění instalační skripty – zkontroluj ji ručně"))
    bin_meta = meta.get("bin")
    if isinstance(bin_meta, dict) and (entry.get("bin") or {}) != bin_meta:
        raise Refused(_("nová verze mění spustitelné soubory (bin)"))


def _check_dependents(packages: dict[str, Any], paths: list[str], t: Target) -> None:
    for origin, entry in packages.items():
        if not isinstance(entry, dict):
            continue
        keys = (*_DEP_KEYS, "devDependencies") if _is_project(origin) else _DEP_KEYS
        for key in keys:
            spec = (entry.get(key) or {}).get(t.name)
            if spec is None or resolve(packages, origin, t.name) not in paths:
                continue
            who = origin or "package.json"
            try:
                ok = semver.satisfies(t.fixed_in, str(spec))
            except semver.Unsupported:
                raise Refused(
                    _("nelze ověřit omezení „{spec}“ z {who}").format(spec=spec, who=who)
                ) from None
            if not ok:
                raise Refused(
                    _("{who} vyžaduje {name} {spec}, opravená verze {version} to nesplňuje").format(
                        who=who, name=t.name, spec=spec, version=t.fixed_in
                    )
                )
