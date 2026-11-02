"""`uv.lock`: přepis verze jednoho balíčku z PyPI podle metadat PyPI JSON API.

`uv.lock` u balíčku z registru nese verzi, odkaz, hash, velikost a čas nahrání souborů
(`sdist`, `wheels`). Přepíšeme přesně tyhle řádky. Aby výstup vypadal jako od `uv`, vykreslí se
nejdřív **stará** verze z jejích metadat a výsledek se musí shodovat s tím, co v lockfilu je
(pořadí, formát času, výběr wheelů podle `requires-python`). Neshoduje-li se, nic se nezapíše.
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from repo_doctor.i18n import _
from repo_doctor.lockfiles import Refused

if TYPE_CHECKING:
    from repo_doctor.lockfiles import Registry, Target

PYPI_SIMPLE = "https://pypi.org/simple"
# omezení prostředí a forky: sada wheelů se pak liší podle prostředí, nepřepisujeme
_ENV_KEYS = (
    "environments",
    "required-environments",
    "supported-markers",
    "required-markers",
    "resolution-markers",
)


def _fmt_time(iso: str) -> str:
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC)
    fraction = f"{stamp.microsecond // 1000:03d}".rstrip("0")  # uv zkracuje na milisekundy
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + (f".{fraction}" if fraction else "") + "Z"


def _inline(f: dict[str, Any], *, with_time: bool) -> str:
    digest = (f.get("digests") or {}).get("sha256")
    parts = [f'url = "{f["url"]}"', f'hash = "sha256:{digest}"', f"size = {int(f['size'])}"]
    if with_time:
        parts.append(f'upload-time = "{_fmt_time(str(f["upload_time_iso_8601"]))}"')
    return "{ " + ", ".join(parts) + " }"


def _tag_ok(interpreter: str, abi: str, spec: SpecifierSet | None) -> bool:
    m = re.fullmatch(r"([a-z]+)(\d)(\d*)", interpreter)
    if not m or m[2] != "3":
        return False
    if not m[3]:  # py3
        return True
    minor = int(m[3])
    if spec is None:
        return True
    if abi == "abi3" or m[1] == "py":  # funguje i na novějších verzích
        return any(spec.contains(f"3.{n}.99") for n in range(minor, 40))
    return spec.contains(f"3.{minor}.99")


def _compatible(filename: str, spec: SpecifierSet | None) -> bool:
    try:
        tags = parse_wheel_filename(filename)[3]
    except InvalidWheelFilename:
        return False
    return any(_tag_ok(tag.interpreter, tag.abi, spec) for tag in tags)


def _files(
    meta: dict[str, Any], spec: SpecifierSet | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(sdist, wheely kompatibilní s `requires-python` seřazené podle názvu souboru)."""
    urls = [f for f in meta.get("urls") or [] if isinstance(f, dict)]
    sdists = [f for f in urls if f.get("packagetype") == "sdist"]
    wheels = [
        f
        for f in urls
        if f.get("packagetype") == "bdist_wheel" and _compatible(str(f.get("filename")), spec)
    ]
    return (sdists[0] if sdists else None), sorted(wheels, key=lambda f: str(f["filename"]))


def _render(
    sdist: dict[str, Any] | None, wheels: list[dict[str, Any]], *, with_time: bool
) -> tuple[str, str]:
    sdist_line = f"sdist = {_inline(sdist, with_time=with_time)}" if sdist else ""
    if not wheels:
        return sdist_line, ""
    body = "".join(f"    {_inline(w, with_time=with_time)},\n" for w in wheels)
    return sdist_line, f"wheels = [\n{body}]"


def _requirements(meta: dict[str, Any]) -> list[Requirement]:
    try:
        return [Requirement(r) for r in (meta.get("info") or {}).get("requires_dist") or []]
    except InvalidRequirement as err:
        raise Refused(_("metadata balíčku nejdou přečíst")) from err


def _signature(reqs: list[Requirement]) -> set[tuple[str, str, tuple[str, ...]]]:
    return {
        (str(canonicalize_name(r.name)), str(r.marker or ""), tuple(sorted(r.extras))) for r in reqs
    }


def _find_block(text: str, name: str, version: str) -> tuple[int, int]:
    heads = list(
        re.finditer(r'^\[\[package\]\]\nname = "([^"\n]*)"\nversion = "([^"\n]*)"\n', text, re.M)
    )
    for i, m in enumerate(heads):
        if canonicalize_name(m[1]) == name and m[2] == version:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return m.start(), end
    raise Refused(_("balíček v lockfilu nelze najít"))


def update(text: str, t: Target, registry: Registry) -> tuple[str, str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise Refused(_("lockfile není platný TOML")) from err
    if data.get("version") != 1:
        raise Refused(_("podporovaný je jen uv.lock ve formátu version = 1"))
    if any(k in data for k in _ENV_KEYS):
        raise Refused(
            _("lockfile omezuje prostředí nebo má větve řešení – bezpečně ho přepsat nelze")
        )
    if (data.get("options") or {}).get("exclude-newer"):
        raise Refused(_("lockfile používá exclude-newer – nová verze by ho mohla porušit"))
    try:
        spec = SpecifierSet(data["requires-python"]) if data.get("requires-python") else None
        new, old = Version(t.fixed_in), Version(t.version)
    except (InvalidSpecifier, InvalidVersion) as err:
        raise Refused(_("verze nebo requires-python nejdou přečíst")) from err
    if new <= old or new.is_prerelease:
        raise Refused(
            _("opravená verze {version} není vyšší běžná verze").format(version=t.fixed_in)
        )
    name = str(canonicalize_name(t.name))
    packages = [p for p in data.get("package") or [] if isinstance(p, dict)]
    mine = [p for p in packages if canonicalize_name(str(p.get("name"))) == name]
    if len(mine) != 1 or mine[0].get("version") != t.version:
        raise Refused(
            _("{name} {version} v lockfilu není (nebo je ve více verzích)").format(
                name=t.name, version=t.version
            )
        )
    if (mine[0].get("source") or {}).get("registry") != PYPI_SIMPLE:
        raise Refused(_("balíček nepochází z PyPI (jiný index, git, soubor)"))

    old_meta = registry.pypi_version(t.name, t.version)
    new_meta = registry.pypi_version(t.name, t.fixed_in)
    start, end = _find_block(text, name, t.version)
    block = text[start:end]
    sub = re.search(r"^\[package\.", block, re.M)
    main = block[: sub.start()] if sub else block

    # 1. formát: vykreslení staré verze se musí shodit s lockfilem
    with_time = "upload-time" in main
    old_sdist_line, old_wheels = _render(*_files(old_meta, spec), with_time=with_time)
    cur_sdist = re.search(r"^sdist = \{.*\}$", main, re.M)
    cur_wheels = re.search(r"^wheels = \[\n.*?^\]$", main, re.M | re.S)
    if (cur_sdist.group(0) if cur_sdist else "") != old_sdist_line or (
        cur_wheels.group(0) if cur_wheels else ""
    ) != old_wheels:
        raise Refused(
            _("soubory verze {version} neodpovídají lockfilu – formát nelze ověřit").format(
                version=t.version
            )
        )

    # 2. závislosti nové verze musí být stejné a splněné tím, co je v lockfilu
    new_reqs = _requirements(new_meta)
    if _signature(_requirements(old_meta)) != _signature(new_reqs):
        raise Refused(_("nová verze má jiné závislosti – strom by bylo nutné přepočítat"))
    locked: dict[str, list[str]] = {}
    for p in packages:
        locked.setdefault(str(canonicalize_name(str(p.get("name")))), []).append(
            str(p.get("version"))
        )
    for req in new_reqs:
        if req.url:
            raise Refused(_("nová verze závisí na přímém odkazu"))
        have = locked.get(str(canonicalize_name(req.name)), [])
        if any(not req.specifier.contains(v, prereleases=True) for v in have):
            raise Refused(_("nová verze vyžaduje {req}, v lockfilu je jiná verze").format(req=req))

    # 3. ostatní balíčky nesmí novou verzi zakazovat
    _check_dependents(packages, mine[0], name, t, registry)

    # 4. přepis
    new_sdist_line, new_wheels = _render(*_files(new_meta, spec), with_time=with_time)
    if bool(new_sdist_line) != bool(old_sdist_line) or bool(new_wheels) != bool(old_wheels):
        raise Refused(_("nová verze má jinou sadu souborů (sdist / wheely) – nelze přepsat"))
    new_main = main.replace(f'\nversion = "{t.version}"\n', f'\nversion = "{t.fixed_in}"\n', 1)
    if cur_sdist:
        new_main = new_main.replace(cur_sdist.group(0), new_sdist_line, 1)
    if cur_wheels:
        new_main = new_main.replace(cur_wheels.group(0), new_wheels, 1)
    return text[:start] + new_main + block[len(main) :] + text[end:], t.fixed_in


def _dep_names(p: dict[str, Any]) -> set[str]:
    refs: list[Any] = list(p.get("dependencies") or [])
    for key in ("optional-dependencies", "dev-dependencies"):
        for group in (p.get(key) or {}).values():
            refs.extend(group or [])
    return {str(canonicalize_name(str(r.get("name")))) for r in refs if isinstance(r, dict)}


def _check_dependents(
    packages: list[dict[str, Any]], mine: dict[str, Any], name: str, t: Target, registry: Registry
) -> None:
    for p in packages:
        if p is mine or name not in _dep_names(p):
            continue
        who = str(p.get("name"))
        source = p.get("source") or {}
        specs: list[SpecifierSet] = []
        if source.get("registry") == PYPI_SIMPLE:
            meta = registry.pypi_version(who, str(p.get("version")))
            specs = [r.specifier for r in _requirements(meta) if canonicalize_name(r.name) == name]
        elif any(k in source for k in ("virtual", "editable", "directory")):
            metadata = p.get("metadata") or {}
            items = list(metadata.get("requires-dist") or [])
            for group in (metadata.get("requires-dev") or {}).values():
                items.extend(group or [])
            try:
                specs = [
                    SpecifierSet(str(i["specifier"]))
                    for i in items
                    if isinstance(i, dict)
                    and canonicalize_name(str(i.get("name"))) == name
                    and i.get("specifier")
                ]
            except InvalidSpecifier as err:
                raise Refused(_("omezení z {who} nejde přečíst").format(who=who)) from err
        else:
            raise Refused(_("nelze ověřit omezení z {who} (zdroj není registr)").format(who=who))
        for spec in specs:
            if not spec.contains(t.fixed_in, prereleases=True):
                raise Refused(
                    _("{who} vyžaduje {name}{spec}, opravená verze {version} to nesplňuje").format(
                        who=who, name=t.name, spec=spec, version=t.fixed_in
                    )
                )
