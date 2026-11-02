"""Aktualizace: kontrola nové verze na GitHub Releases a samoaktualizace instalace z tarballu.

Způsob instalace určuje soubor `install-kind` vedle spustitelného souboru, který tam zapíše
balíček (`deb`, `rpm`, `pacman`) nebo tarball (`tarball`). Balíčky aktualizuje správce
balíčků systému (APT/DNF repozitář, AUR) – repo-doctor jen poradí příkaz. Instalaci
z tarballu umí `repo-doctor update` nahradit sám, ale jen po ověření podpisu `SHA256SUMS`
klíčem zabudovaným v aplikaci (otisk `FINGERPRINT`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version

from repo_doctor import __version__, paths
from repo_doctor.i18n import _

log = logging.getLogger(__name__)

GITHUB_REPO = "Icenux/repo-doctor"
LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
DOWNLOAD_BASE = f"https://github.com/{GITHUB_REPO}/releases/download"
TARBALL = "repo-doctor-linux-x86_64.tar.gz"
SUMS = "SHA256SUMS"
SUMS_SIG = "SHA256SUMS.asc"
# Otisk podpisového klíče vydání (veřejná část je v data/signing-key.asc).
FINGERPRINT = "61CB544785F6CE13358165CDDD0ADC39ACA4DB9A"
CHECK_INTERVAL_S = 24 * 3600
MAX_DOWNLOAD = 300 * 1024 * 1024
KIND_FILE = "install-kind"
OLD_DIR = ".repo-doctor-old"


class UpdateError(Exception):
    """Aktualizace nejde zjistit nebo provést; zpráva je pro uživatele."""


class InstallKind(StrEnum):
    DEB = "deb"
    RPM = "rpm"
    PACMAN = "pacman"
    TARBALL = "tarball"
    SOURCE = "source"  # spuštěno z Pythonu (vývoj), ne ze sestavené aplikace


@dataclass(frozen=True)
class Release:
    version: Version
    tag: str


def current_version() -> Version:
    return Version(__version__)


def app_dir() -> Path:
    """Složka sestavené aplikace (PyInstaller onedir) – v ní je binárka i `install-kind`."""
    return Path(sys.executable).resolve().parent


def install_kind(base: Path | None = None) -> InstallKind:
    if base is None:
        if not getattr(sys, "frozen", False):
            return InstallKind.SOURCE
        base = app_dir()
    try:
        return InstallKind((base / KIND_FILE).read_text("utf-8").strip())
    except (OSError, ValueError):
        return InstallKind.SOURCE


def instructions(kind: InstallKind) -> str:
    return {
        InstallKind.DEB: _("přijde s aktualizací systému (sudo apt update && sudo apt upgrade)"),
        InstallKind.RPM: _("přijde s aktualizací systému (sudo dnf upgrade)"),
        InstallKind.PACMAN: _("aktualizuj přes AUR helper (např. yay -Syu)"),
        InstallKind.TARBALL: _("spusť repo-doctor update"),
        InstallKind.SOURCE: _("vývojová instalace – stáhni změny (git pull)"),
    }[kind]


def _client() -> httpx.Client:
    from repo_doctor.httpclient import USER_AGENT

    return httpx.Client(
        timeout=httpx.Timeout(15.0, read=60.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def fetch_latest(client: httpx.Client | None = None) -> Release:
    """Poslední vydaná verze (bez předběžných) podle GitHub API."""
    own = client is None
    http = client or _client()
    try:
        resp = http.get(LATEST_API, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code == 404:
            raise UpdateError(_("Zatím není vydaná žádná verze."))
        resp.raise_for_status()
        tag = str(resp.json().get("tag_name", ""))
        return Release(Version(tag.removeprefix("v")), tag)
    except httpx.HTTPError as err:
        raise UpdateError(
            _("GitHub Releases nejsou dostupné: {error}").format(error=type(err).__name__)
        ) from None
    except (InvalidVersion, ValueError, AttributeError):
        raise UpdateError(_("GitHub vrátil nečekanou odpověď o vydáních.")) from None
    finally:
        if own:
            http.close()


# ---------------------------------------------------------------------- kontrola na pozadí
def _cache_file() -> Path:
    return paths.cache_dir() / "update-check.json"


def latest_cached(
    fetch: Callable[[], Release] = fetch_latest,
    *,
    now: Callable[[], float] = time.time,
    cache: Path | None = None,
) -> Version | None:
    """Nejnovější verze – ze sítě nejvýš jednou za `CHECK_INTERVAL_S`, jinak z cache."""
    file = cache or _cache_file()
    with contextlib.suppress(OSError, ValueError, InvalidVersion, KeyError, TypeError):
        data = json.loads(file.read_text("utf-8"))
        if 0 <= now() - float(data["checked"]) < CHECK_INTERVAL_S:
            return Version(str(data["latest"]))
    try:
        release = fetch()
    except UpdateError as err:
        log.debug("update check failed: %s", err)
        return None
    with contextlib.suppress(OSError):
        paths.ensure_private_dir(file.parent)
        file.write_text(json.dumps({"checked": now(), "latest": str(release.version)}), "utf-8")
    return release.version


def notice(
    fetch: Callable[[], Release] = fetch_latest,
    *,
    kind: InstallKind | None = None,
    now: Callable[[], float] = time.time,
    cache: Path | None = None,
) -> str | None:
    """Text upozornění pro TUI, je-li venku novější verze; jinak None (i při chybě sítě)."""
    latest = latest_cached(fetch, now=now, cache=cache)
    if latest is None or latest <= current_version():
        return None
    how = instructions(kind or install_kind())
    return _("Je dostupná verze {latest} (máš {current}) – {how}.").format(
        latest=latest, current=__version__, how=how
    )


# ---------------------------------------------------------------------- ověření podpisu
def verify_signature(
    data: Path, signature: Path, *, key: bytes | None = None, fingerprint: str = FINGERPRINT
) -> None:
    """Ověří odpojený OpenPGP podpis přes gpgv/gpg proti zabudovanému klíči (jen tomu)."""
    gpg = shutil.which("gpg")
    if gpg is None:
        raise UpdateError(_("Ověření podpisu potřebuje GnuPG – nainstaluj balíček gnupg."))
    if key is None:
        key = (Path(__file__).resolve().parent / "data" / "signing-key.asc").read_bytes()
    with tempfile.TemporaryDirectory(prefix="repo-doctor-gpg-") as tmp:
        home = Path(tmp) / "home"
        home.mkdir(mode=0o700)
        armored = Path(tmp) / "key.asc"
        armored.write_bytes(key)
        keyring = Path(tmp) / "key.gpg"
        base = [gpg, "--homedir", str(home), "--batch", "--quiet"]
        _gpg_run([*base, "--dearmor", "--output", str(keyring), str(armored)])
        gpgv = shutil.which("gpgv")
        if gpgv is not None:
            cmd = [gpgv, "--homedir", str(home), "--status-fd", "1", "--keyring", str(keyring)]
        else:
            cmd = [*base, "--no-default-keyring", "--keyring", str(keyring), "--status-fd", "1"]
            cmd.append("--verify")
        proc = _gpg_run([*cmd, str(signature), str(data)], check=False)
    want = fingerprint.upper()
    for line in proc.stdout.splitlines():
        parts = line.split()
        valid = parts[:2] == ["[GNUPG:]", "VALIDSIG"] and want in (parts[2], parts[-1])
        if valid and proc.returncode == 0:
            return
    raise UpdateError(_("Podpis aktualizace neplatí – stažené soubory nepoužiju."))


def _gpg_run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise UpdateError(f"GnuPG nejde spustit: {type(err).__name__}") from None
    if check and proc.returncode != 0:
        raise UpdateError(_("GnuPG nezvládl načíst podpisový klíč."))
    return proc


# ---------------------------------------------------------------------- samoaktualizace
def _download(http: httpx.Client, url: str, target: Path) -> None:
    size = 0
    with http.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise UpdateError(
                _("Stažení {name} selhalo (HTTP {status_code}).").format(
                    name=url.rsplit("/", 1)[-1], status_code=resp.status_code
                )
            )
        with target.open("wb") as out:
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise UpdateError(_("Stahovaný soubor je nečekaně velký – přerušeno."))
                out.write(chunk)


def _expected_sha(sums: Path, name: str) -> str:
    for line in sums.read_text("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise UpdateError(_("{name} chybí v podepsaném seznamu kontrolních součtů.").format(name=name))


def _sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def self_update(
    release: Release,
    *,
    target: Path | None = None,
    client: httpx.Client | None = None,
    verify: Callable[[Path, Path], None] = verify_signature,
    progress: Callable[[str], None] = lambda _msg: None,
) -> None:
    """Stáhne tarball vydání, ověří podpis a kontrolní součet a vymění složku aplikace.

    Výměna je dvojí přejmenování ve stejné nadřazené složce; při chybě se vrací původní stav.
    Stará verze zůstane v `.repo-doctor-old` (běžící proces z ní může ještě načítat moduly)
    a smaže se při další aktualizaci. Nová verze platí od dalšího spuštění.
    """
    target = target or app_dir()
    if release.version <= current_version():
        raise UpdateError(
            _("Verze {version} není novější než {current}.").format(
                version=release.version, current=__version__
            )
        )
    if install_kind(target) is not InstallKind.TARBALL:
        raise UpdateError("Samoaktualizace je jen pro instalaci z tarballu.")
    parent = target.parent
    if not os.access(parent, os.W_OK) or not os.access(target, os.W_OK):
        raise UpdateError(f"Do {target} nejde zapisovat.")
    own = client is None
    http = client or _client()
    base = f"{DOWNLOAD_BASE}/{release.tag}"
    old = parent / OLD_DIR
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".repo-doctor-update-", dir=parent) as tmp:
            work = Path(tmp)
            progress(_("Stahuji seznam kontrolních součtů…"))
            _download(http, f"{base}/{SUMS}", work / SUMS)
            _download(http, f"{base}/{SUMS_SIG}", work / SUMS_SIG)
            progress(_("Ověřuji podpis…"))
            verify(work / SUMS, work / SUMS_SIG)
            expected = _expected_sha(work / SUMS, TARBALL)
            progress(f"Stahuji {TARBALL}…")
            _download(http, f"{base}/{TARBALL}", work / TARBALL)
            if _sha256(work / TARBALL) != expected:
                raise UpdateError(_("Kontrolní součet staženého balíku nesedí."))
            progress("Rozbaluji…")
            fresh = _extract(work / TARBALL, work / "new")
            # Verzi kryje podepsaný součet tarballu – podvržená odpověď API tak nemůže
            # vrátit starší (i když platně podepsané) vydání.
            packaged = _packaged_version(fresh)
            if packaged != release.version:
                raise UpdateError(
                    _("Balík obsahuje verzi {packaged}, ne {version}.").format(
                        packaged=packaged, version=release.version
                    )
                )
            _swap(target, fresh, old)
    except httpx.HTTPError as err:
        raise UpdateError(
            _("Stahování selhalo: {error}").format(error=type(err).__name__)
        ) from None
    finally:
        if own:
            http.close()


def _extract(archive: Path, dest: Path) -> Path:
    dest.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as err:
        raise UpdateError(
            _("Balík nejde rozbalit: {error}").format(error=type(err).__name__)
        ) from None
    fresh = dest / "repo-doctor"
    binary = fresh / "repo-doctor"
    if not binary.is_file() or install_kind(fresh) is not InstallKind.TARBALL:
        raise UpdateError(_("Balík nemá očekávaný obsah."))
    binary.chmod(0o755)
    return fresh


def _packaged_version(fresh: Path) -> Version | None:
    try:
        return Version((fresh / "VERSION").read_text("utf-8").strip())
    except (OSError, InvalidVersion):
        return None


def _swap(target: Path, fresh: Path, old: Path) -> None:
    target.rename(old)
    try:
        fresh.rename(target)
    except OSError:
        old.rename(target)
        raise
