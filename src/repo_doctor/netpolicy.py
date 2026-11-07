"""Které hosty smí repo-doctor oslovit nešifrovaným `http://` (volba `allow_http_hosts`).

Výchozí stav je prázdný seznam: síť jen přes `https` a `ssh`. Uživatel může v globální
konfiguraci povolit `http` pro konkrétní hosty – typicky self-hosted Forgejo v síti
Tailscale, kde šifrování zajišťuje VPN. Pravidla platí pro git (fetch, clone, ls-remote)
i pro HTTP API hostingů. Cizí repo si `http` povolit nemůže (`.repo-doctor.toml` volbu nezná).

Položka seznamu je jedno z:
- přesný hostname (`git.tailnet-name.ts.net`),
- zástupný prefix `*.` pro subdomény (`*.ts.net` – shoduje se s `a.ts.net`, ne s `ts.net`),
- IPv4/IPv6 adresa (`100.101.102.103`, `fd7a:115c:a1e0::1`),
- síť v zápisu CIDR (`100.64.0.0/10`).

Port se neuvádí, položka platí pro jakýkoli port daného hosta.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from repo_doctor.i18n import _

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# Vypadá jako číselná adresa (např. `100.1`, `0x64.1`), ale není to standardní zápis IP –
# takové „hostname“ curl/git převede na IP po svém, proto ho nikdy neuznáváme.
_NUMERIC_LIKE = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$")


def normalize_host(host: str) -> str | IPAddress | None:
    """Host z URL na porovnatelný tvar: IP adresa, nebo hostname malými písmeny v punycode.

    Vrátí None pro prázdný nebo neplatný host a pro nestandardní číselné zápisy.
    """
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        return None
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    host = host.rstrip(".").lower()
    if not host or ":" in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if _NUMERIC_LIKE.fullmatch(host):
        return None
    labels = host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        return None
    return host


@dataclass(frozen=True)
class HostRule:
    kind: str  # "host" | "suffix" | "ip" | "net"
    value: str | IPAddress | IPNetwork
    text: str

    def matches(self, host: str | IPAddress) -> bool:
        if isinstance(host, str):
            if self.kind == "host":
                return host == self.value
            if self.kind == "suffix":
                return host.endswith("." + str(self.value))
            return False
        if self.kind == "ip":
            return host == self.value
        if self.kind == "net":
            net = self.value
            assert isinstance(net, ipaddress.IPv4Network | ipaddress.IPv6Network)  # noqa: S101
            return host.version == net.version and host in net
        return False


def parse_rule(entry: str) -> HostRule:
    """Ověří a převede položku `allow_http_hosts`. Chyba je česká věta pro uživatele."""
    text = entry.strip()
    if not text:
        raise ValueError(_("prázdná položka"))
    if "://" in text:
        raise ValueError(_("{text!r}: uveď jen host, ne URL").format(text=text))
    if text in ("*", "*.", "*.*"):
        raise ValueError(
            _("{text!r}: zástupný znak pro všechny hosty není povolený").format(text=text)
        )
    if "/" in text:
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as err:
            raise ValueError(_("{text!r}: neplatná síť CIDR").format(text=text)) from err
        if net.prefixlen == 0:
            raise ValueError(
                _("{text!r}: síť pokrývající celý internet není povolená").format(text=text)
            )
        return HostRule("net", net, text)
    if "*" in text:
        if not text.startswith("*.") or "*" in text[2:]:
            raise ValueError(
                _("{text!r}: zástupný znak jde použít jen jako prefix `*.`").format(text=text)
            )
        domain = normalize_host(text[2:])
        if not isinstance(domain, str):
            raise ValueError(_("{text!r}: neplatná doména za `*.`").format(text=text))
        if "." not in domain:
            raise ValueError(
                _("{text!r}: `*.` musí následovat doména, ne celé TLD").format(text=text)
            )
        return HostRule("suffix", domain, text)
    bare = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    if re.fullmatch(r"[^:]+:\d+", bare) or re.fullmatch(r"\[.+\]:\d+", text):
        raise ValueError(
            _("{text!r}: port se neuvádí (položka platí pro všechny porty)").format(text=text)
        )
    host = normalize_host(text)
    if host is None:
        raise ValueError(_("{text!r}: neplatný hostname nebo IP adresa").format(text=text))
    if isinstance(host, str):
        return HostRule("host", host, text)
    return HostRule("ip", host, text)


def validate_entries(entries: Iterable[str]) -> list[str]:
    """Pro pydantic validátor: vrátí očištěné položky, nebo vyhodí ValueError."""
    out: list[str] = []
    for entry in entries:
        parse_rule(entry)
        out.append(entry.strip())
    return out


class HttpHostPolicy:
    def __init__(self, entries: Iterable[str] = ()) -> None:
        self.rules = [parse_rule(e) for e in entries]

    def __bool__(self) -> bool:
        return bool(self.rules)

    def allows_host(self, host: str | None) -> bool:
        if not host or not self.rules:
            return False
        norm = normalize_host(host)
        if norm is None:
            return False
        return any(rule.matches(norm) for rule in self.rules)

    def allows_http_url(self, url: str) -> bool:
        """Smí repo-doctor oslovit tuto URL přes `http://`? (jiná schémata → False)"""
        try:
            parts = urlsplit(url.strip())
            host = parts.hostname
        except ValueError:
            return False
        return parts.scheme.lower() == "http" and self.allows_host(host)


_LOCK = threading.Lock()
_policy = HttpHostPolicy()


def configure(entries: Iterable[str]) -> None:
    """Nastaví pravidla z globální konfigurace (CLI i TUI při načtení konfigurace)."""
    global _policy
    policy = HttpHostPolicy(entries)
    with _LOCK:
        _policy = policy


def policy() -> HttpHostPolicy:
    with _LOCK:
        return _policy


def is_http(url: str) -> bool:
    return url.strip().lower().startswith("http://")


def http_allowed_for(urls: Iterable[str]) -> bool:
    """Povolit `http` pro příkaz gitu, který se může připojit na tyto URL?

    Ano jen tehdy, když aspoň jedna URL je `http://` a **všechny** `http://` URL mají host
    ze seznamu. URL přes https/ssh seznam nepotřebují – zůstávají šifrované (přesměrování
    se u příkazů s povoleným http nenásledují, takže se na http „nepřepnou“).
    """
    pol = policy()
    http_urls = [u for u in urls if is_http(u)]
    return bool(http_urls) and all(pol.allows_http_url(u) for u in http_urls)


def http_allow_advice(host: str | None = None, settings_key: str | None = None) -> str:
    """Jednotný návod, jak povolit nešifrovaný http pro host (hlášky `insecure_reason` a SSH/git).

    `host` je hodnota k vložení (bez portu a bez `http://`), `settings_key` klávesa obrazovky
    Nastavení z keymapu (CLI ji nezná, pak se odkazuje na `config.toml`).
    """
    what = _("host {host}").format(host=host) if host else _("jeho host")
    where = (
        _("v Nastavení (klávesa {key})").format(key=settings_key)
        if settings_key
        else _("v Nastavení nebo v config.toml")
    )
    return _(
        "Pokud je server v důvěryhodné síti (Tailscale), přidej {what} do pole „Hosty s povoleným "
        "nešifrovaným http“ (allow_http_hosts) {where}. Zadej jen host, bez portu a bez http://."
    ).format(what=what, where=where)
