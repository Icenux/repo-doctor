"""Asynchronní HTTP klient: omezený souběh, cache s TTL a ETagem, stránkování, rate-limit.

Nikdy neloguje ani necachuje hlavičky (tedy ani tokeny). Chyby nesou jen URL bez query.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from repo_doctor import __version__, netpolicy
from repo_doctor.i18n import _
from repo_doctor.masking import redact
from repo_doctor.paths import ensure_private_dir

log = logging.getLogger(__name__)

USER_AGENT = f"repo-doctor/{__version__} (+https://github.com/Icenux/repo-doctor)"


class HttpError(Exception):
    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        self.status = status
        self.url = url.split("?", 1)[0]
        super().__init__(redact(message))


class AuthError(HttpError):
    """401 – neplatný nebo expirovaný token."""


class ForbiddenError(HttpError):
    """403 – token nemá oprávnění."""


class NotFoundError(HttpError):
    """404."""


class RateLimitError(HttpError):
    """Vyčerpaný rate-limit a čekání by trvalo příliš dlouho."""


class UnavailableError(HttpError):
    """Instance nedostupná (síť, TLS, timeout, 5xx)."""


class InsecureHostError(HttpError):
    """Požadavek přes nešifrovaný http na host, který není v `allow_http_hosts`."""


@dataclass
class CachedResponse:
    status: int
    body: Any
    etag: str | None
    ts: float
    link_next: str | None = None


class ResponseCache:
    """Cache odpovědí v paměti a volitelně na disku (JSON soubory, práva 0600)."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self._mem: dict[str, CachedResponse] = {}

    @staticmethod
    def key(namespace: str, method: str, url: str, body: bytes | None = None) -> str:
        h = hashlib.sha256()
        for part in (namespace, method, url):
            h.update(part.encode())
            h.update(b"\0")
        if body:
            h.update(body)
        return h.hexdigest()

    def get(self, key: str) -> CachedResponse | None:
        if key in self._mem:
            return self._mem[key]
        if self.directory is None:
            return None
        file = self.directory / f"{key}.json"
        try:
            data = json.loads(file.read_text("utf-8"))
            entry = CachedResponse(**data)
        except (OSError, ValueError, TypeError):
            return None
        self._mem[key] = entry
        return entry

    def set(self, key: str, entry: CachedResponse) -> None:
        self._mem[key] = entry
        if self.directory is None:
            return
        try:
            ensure_private_dir(self.directory)
            file = self.directory / f"{key}.json"
            tmp = file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.__dict__))
            tmp.replace(file)
        except OSError as err:  # pragma: no cover - cache je jen optimalizace
            log.debug("cache nelze zapsat: %s", err)


SENSITIVE_HEADERS = ("authorization", "private-token", "job-token", "cookie")


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _parse_link_next(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        if any(s.strip() in ('rel="next"', "rel=next") for s in section[1:]):
            return url
    return None


def _error_detail(resp: httpx.Response) -> str:
    """Zpráva z těla chyby API (`message`), zkrácená; prázdná, pokud tělo není JSON."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    if message is None:
        return ""
    text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
    return " ".join(text.split())[:200]


def build_verify(verify_tls: bool, ca_bundle: str | None) -> bool | ssl.SSLContext:
    if ca_bundle:
        from repo_doctor.paths import expand_path

        return ssl.create_default_context(cafile=str(expand_path(ca_bundle)))
    return verify_tls


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str = "",
        headers: Mapping[str, str] | None = None,
        namespace: str = "public",
        cache: ResponseCache | None = None,
        ttl: float = 900,
        timeout: float = 15.0,
        max_concurrency: int = 8,
        verify: bool | ssl.SSLContext = True,
        max_rate_wait: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.namespace = namespace
        self.cache = cache or ResponseCache()
        self.ttl = ttl
        self.max_rate_wait = max_rate_wait
        self._sleep = sleep
        self._sem = asyncio.Semaphore(max_concurrency)
        self._origin = _origin(httpx.URL(base_url)) if base_url else None
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
            timeout=timeout,
            verify=verify,
            follow_redirects=True,
            transport=transport,
            event_hooks={"request": [self._guard_credentials]},
        )

    async def _guard_credentials(self, request: httpx.Request) -> None:
        """Přihlašovací hlavičky smí odejít jen na origin hostingu (ne po přesměrování jinam).

        httpx při přesměrování na cizí origin maže jen `Authorization`, ne např. GitLabí
        `PRIVATE-TOKEN` – proto to hlídáme sami u každého požadavku.
        """
        if request.url.scheme == "http" and not netpolicy.policy().allows_host(request.url.host):
            # Nešifrovaný http jen na hosty z `allow_http_hosts` – platí i pro přesměrování,
            # takže token ani jiná data nikdy neodejdou po http jinam.
            raise InsecureHostError(
                _("http://{host} není v allow_http_hosts – nešifrované spojení odmítnuto").format(
                    host=request.url.host
                ),
                url=str(request.url),
            )
        if self._origin is not None and _origin(request.url) != self._origin:
            for name in SENSITIVE_HEADERS:
                request.headers.pop(name, None)
        self.last_headers: httpx.Headers = httpx.Headers()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, url: str) -> str:
        return str(self._client.build_request("GET", url).url)

    async def _send(
        self, method: str, url: str, *, headers: dict[str, str], content: bytes | None
    ) -> httpx.Response:
        attempts = 0
        while True:
            attempts += 1
            try:
                async with self._sem:
                    resp = await self._client.request(method, url, headers=headers, content=content)
            except httpx.TimeoutException as err:
                raise UnavailableError(
                    _("časový limit při dotazu na {url}").format(url=url.split("?")[0]), url=url
                ) from err
            except httpx.HTTPError as err:
                raise UnavailableError(
                    _("nedostupné: {error} ({url})").format(
                        error=type(err).__name__, url=url.split("?")[0]
                    ),
                    url=url,
                ) from err
            wait = self._rate_limit_wait(resp)
            if wait is None:
                return resp
            if wait > self.max_rate_wait or attempts > 2:
                raise RateLimitError(
                    _("vyčerpaný rate-limit (další pokus za {wait:.0f} s)").format(wait=wait),
                    status=resp.status_code,
                    url=url,
                )
            await self._sleep(wait)

    @staticmethod
    def _rate_limit_wait(resp: httpx.Response) -> float | None:
        remaining = resp.headers.get("x-ratelimit-remaining") or resp.headers.get(
            "ratelimit-remaining"
        )
        limited = resp.status_code == 429 or (resp.status_code == 403 and remaining == "0")
        if not limited:
            return None
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                except (TypeError, ValueError):
                    return 60.0
        reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("ratelimit-reset")
        if reset and reset.isdigit():
            value = int(reset)
            return max(0.0, value - time.time()) if value > 1_000_000_000 else float(value)
        return 60.0

    @staticmethod
    def _raise_for(resp: httpx.Response, url: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        short = url.split("?")[0]
        if status == 401:
            raise AuthError(_("neplatný nebo expirovaný token (401)"), status=status, url=url)
        if status == 403:
            raise ForbiddenError(
                _("přístup odepřen (403) – {short}").format(short=short), status=status, url=url
            )
        if status == 404:
            raise NotFoundError(
                _("nenalezeno (404) – {short}").format(short=short), status=status, url=url
            )
        if status >= 500:
            raise UnavailableError(
                _("chyba serveru {status} – {short}").format(status=status, short=short),
                status=status,
                url=url,
            )
        detail = _error_detail(resp)
        raise HttpError(
            f"HTTP {status} – {short}" + (f": {detail}" if detail else ""), status=status, url=url
        )

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Any = None,
        ttl: float | None = None,
    ) -> tuple[Any, str | None]:
        """Vrátí (tělo, url další stránky). Odpovědi se cachují podle TTL a revalidují ETagem."""
        full = self._url(url)
        if params:
            full = str(httpx.URL(full).copy_merge_params(dict(params)))
        content = json.dumps(json_body, sort_keys=True).encode() if json_body is not None else None
        key = self.cache.key(self.namespace, method, full, content)
        ttl = self.ttl if ttl is None else ttl
        cached = self.cache.get(key)
        now = time.time()
        if cached and ttl > 0 and now - cached.ts < ttl:
            return cached.body, cached.link_next
        headers: dict[str, str] = {}
        if content is not None:
            headers["Content-Type"] = "application/json"
        if cached and cached.etag and method == "GET":
            headers["If-None-Match"] = cached.etag
        resp = await self._send(method, full, headers=headers, content=content)
        self.last_headers = resp.headers
        if resp.status_code == 304 and cached:
            cached.ts = now
            self.cache.set(key, cached)
            return cached.body, cached.link_next
        self._raise_for(resp, full)
        try:
            body = resp.json() if resp.content else None
        except ValueError as err:
            raise HttpError(
                _("neplatná JSON odpověď – {url}").format(url=full.split("?")[0]), url=full
            ) from err
        link_next = _parse_link_next(resp.headers.get("link"))
        if ttl > 0:
            self.cache.set(
                key,
                CachedResponse(resp.status_code, body, resp.headers.get("etag"), now, link_next),
            )
        return body, link_next

    async def get_json(
        self, url: str, *, params: Mapping[str, str | int] | None = None, ttl: float | None = None
    ) -> Any:
        body, _ = await self.request_json("GET", url, params=params, ttl=ttl)
        return body

    async def post_json(self, url: str, payload: Any, *, ttl: float | None = None) -> Any:
        body, _ = await self.request_json("POST", url, json_body=payload, ttl=ttl)
        return body

    async def paginate(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        max_pages: int = 50,
        items_key: str | None = None,
        ttl: float | None = None,
    ) -> list[Any]:
        """Projde stránky podle hlavičky Link (rel="next")."""
        items: list[Any] = []
        next_url: str | None = url
        next_params = params
        for _page in range(max_pages):
            if next_url is None:
                break
            body, link = await self.request_json("GET", next_url, params=next_params, ttl=ttl)
            page = body.get(items_key, []) if items_key and isinstance(body, dict) else body
            if not isinstance(page, list):
                break
            items.extend(page)
            if (
                link is not None
                and self._origin is not None
                and _origin(httpx.URL(link)) != self._origin
            ):
                break  # stránkování mimo hosting nenásledujeme
            next_url, next_params = link, None
        return items
