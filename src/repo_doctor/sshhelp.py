"""Srozumitelné české hlášky pro typické SSH chyby gitu.

`explain_ssh_error` je čistá funkce nad stderr a URL remote – nic nespouští a do výsledku
nikdy nedá heslo ani token z URL (jen host, port a bezpečné uživatelské jméno).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from repo_doctor.forges.urls import parse_remote
from repo_doctor.i18n import N_, _
from repo_doctor.masking import redact
from repo_doctor.netpolicy import http_allow_advice

if TYPE_CHECKING:
    from repo_doctor.config import ForgeConfig
    from repo_doctor.forges.base import Forge

_HOST_KEY = re.compile(r"Host key verification failed|No \w+ host key is known", re.I)
_PUBLICKEY = re.compile(r"Permission denied \(publickey", re.I)
_UNREACHABLE = re.compile(
    r"Could not resolve hostname|Connection timed out|Connection refused|"
    r"Operation timed out|No route to host|Network is unreachable",
    re.I,
)
_PROTOCOL = re.compile(r"transport '([A-Za-z0-9+.-]{1,32})' not allowed")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[[0-9A-Fa-f:.]+\]$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

PUBLICKEY_MSG = N_(
    "Server odmítl SSH klíč. Zkontroluj, že je klíč přidaný v účtu na hostingu, "
    "případně `core.sshCommand` / `~/.ssh/config`."
)
UNREACHABLE_MSG = N_("Server není dostupný (síť, VPN/Tailscale, port).")


def _ssh_target(url: str | None) -> str:
    """`ssh [-p port] user@host` pro ruční první připojení (bez hesel a tokenů)."""
    remote = parse_remote(url) if url else None
    host = remote.host if remote and remote.host else None
    if host is None or not _SAFE_HOST.match(host):
        return "ssh git@<server>"
    # uživatel jen u SSH URL – u https je v userinfo typicky token
    user = (
        remote.user
        if remote and remote.scheme == "ssh" and remote.user and _SAFE_USER.match(remote.user)
        else "git"
    )
    port = f"-p {remote.port} " if remote and remote.port else ""
    return f"ssh {port}{user}@{host}"


def explain_ssh_error(stderr: str, url: str | None = None) -> str | None:
    """Česká hláška s postupem pro známou SSH chybu (nebo zakázaný protokol), jinak None."""
    if m := _PROTOCOL.search(stderr):
        msg = _(
            "Remote používá nepovolený protokol {protocol}, repo-doctor ho z bezpečnostních důvodů nefetchuje (povolené jsou https a ssh)."
        ).format(protocol=m.group(1))
        if m.group(1) == "http":
            remote = parse_remote(url) if url else None
            host = remote.host if remote and remote.host and _SAFE_HOST.match(remote.host) else None
            msg += " " + http_allow_advice(host)
        return msg
    if _HOST_KEY.search(stderr):
        return _(
            "Server zatím neznáš. Připoj se k němu jednou ručně: `{target}` a potvrď otisk klíče. Pak to zkus znovu."
        ).format(target=_ssh_target(url))
    if _PUBLICKEY.search(stderr):
        return _(PUBLICKEY_MSG)
    if _UNREACHABLE.search(stderr):
        return _(UNREACHABLE_MSG)
    return None


def friendly_git_error(stderr: str, url: str | None = None) -> str:
    """Přeložená hláška, nebo původní (redigovaný) text, když chybu neznáme."""
    return explain_ssh_error(stderr, url) or redact(stderr)


def _ls_remote(url: str) -> None:
    from repo_doctor.gitwrap import Git

    Git.ls_remote(url)


# Sonda SSH pro „test připojení“; testy ji nahrazují, aby nikdy nešly do sítě.
run_probe: Callable[[str], None] = _ls_remote


def probe_ssh(url: str) -> str | None:
    """Zkusí `git ls-remote` přes SSH; vrátí českou hlášku, pokud selže na SSH vrstvě."""
    from repo_doctor.gitwrap import GitError

    try:
        run_probe(url)
    except GitError as err:
        return explain_ssh_error(err.stderr, url)
    return None


async def forge_ssh_warning(forge: Forge, config: ForgeConfig) -> str | None:
    """Při klonování přes SSH ověří i SSH k hostingu (na URL prvního repa)."""
    import asyncio

    from repo_doctor.httpclient import HttpError

    if config.clone_protocol != "ssh":
        return None
    try:
        repos = await forge.list_repos()
    except HttpError:
        return None
    url = next((r.clone_ssh for r in repos if r.clone_ssh), None)
    if url is None:
        return None
    hint = await asyncio.to_thread(probe_ssh, url)
    return f"SSH: {hint}" if hint else None
