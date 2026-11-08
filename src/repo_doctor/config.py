"""Konfigurace: pydantic modely + načítání/ukládání přes tomlkit (zachová komentáře)."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from tomlkit.items import AoT, Table
from tomlkit.toml_document import TOMLDocument

from repo_doctor import paths
from repo_doctor.i18n import N_, _
from repo_doctor.models import Severity

ForgeType = Literal["github", "gitea", "forgejo", "gitlab"]
TokenSource = Literal["keyring", "env", "cmd", "none"]
LicenseId = Literal["MIT", "ISC", "BSD-2-Clause", "Unlicense"]

DEFAULT_FORGE_URLS: dict[str, str] = {
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com",
}


class ConfigError(Exception):
    """Neplatná konfigurace – zpráva je určená přímo uživateli."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RootConfig(_Model):
    path: str
    depth: int = Field(default=3, ge=0, le=12)
    exclude: list[str] = Field(default_factory=list)
    enabled: bool = True
    follow_symlinks: bool = False

    @field_validator("path")
    @classmethod
    def _path_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(_("cesta nesmí být prázdná"))
        return value.strip()

    @property
    def expanded(self) -> Path:
        return paths.expand_path(self.path)


class ForgeConfig(_Model):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    type: ForgeType
    url: str | None = None
    user: str | None = None
    org: str | None = None
    token_source: TokenSource | None = None
    token_env: str | None = None
    token_cmd: str | None = None
    verify_tls: bool = True
    ca_bundle: str | None = None
    clone_protocol: Literal["ssh", "https"] = "ssh"
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _no_plaintext_token(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("token", "password", "api_key", "secret"):
                if key in data:
                    raise ValueError(
                        _(
                            'klíč `{key}` není povolený: token nepatří do config.toml jako prostý text. Použij token_env, token_cmd nebo token_source = "keyring".'
                        ).format(key=key)
                    )
        return data

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError(_("URL instance musí začínat http:// nebo https://"))
        if "@" in value.split("//", 1)[1].split("/", 1)[0]:
            raise ValueError(_("URL nesmí obsahovat přihlašovací údaje"))
        return value

    @model_validator(mode="after")
    def _source(self) -> ForgeConfig:
        if self.token_source is None:
            if self.token_env:
                self.token_source = "env"
            elif self.token_cmd:
                self.token_source = "cmd"
            else:
                self.token_source = "none"
        if self.token_source == "env" and not self.token_env:
            raise ValueError(_('token_source = "env" vyžaduje token_env (název proměnné)'))
        if self.token_source == "cmd" and not self.token_cmd:
            raise ValueError(_('token_source = "cmd" vyžaduje token_cmd'))
        if self.type in ("gitea", "forgejo") and not self.url:
            raise ValueError(_("hosting typu {type} vyžaduje url instance").format(type=self.type))
        return self

    @property
    def base_url(self) -> str:
        return self.url or DEFAULT_FORGE_URLS.get(self.type, "")


class ChecksConfig(_Model):
    disabled: list[str] = Field(default_factory=list)
    fail_on: Severity = Severity.HIGH


class LimitsConfig(_Model):
    large_file_mb: float = Field(default=5.0, gt=0, le=10_000)
    binary_file_kb: int = Field(default=1024, gt=0)
    stale_branch_days: int = Field(default=90, ge=1)
    stash_days: int = Field(default=30, ge=1)
    no_pulse_days: int = Field(default=90, ge=1)
    stale_pr_days: int = Field(default=30, ge=1)
    history_timeout_s: float = Field(default=60.0, gt=0)
    git_timeout_s: float = Field(default=30.0, gt=0)
    history_size: int = Field(default=30, ge=1, le=1000)
    http_cache_minutes: int = Field(default=15, ge=0)
    registry_cache_hours: int = Field(default=24, ge=0)
    max_file_kb: int = Field(default=1024, gt=0)  # větší soubory skener tajemství přeskočí


class AllowEntry(_Model):
    hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    reason: str = Field(min_length=1)
    check: str | None = None
    repo: str | None = None


# Klíče `[ui]` zrušené v #89 (jediné téma, levý panel bez přepínače); ve starých configech
# se tiše zahodí, aby se kvůli nim aplikace nezastavila.
_REMOVED_UI_KEYS = ("theme", "show_sidebar")


class UIConfig(_Model):
    editor: str | None = None
    group_by: Literal["triage", "root", "forge", "remote"] = "triage"
    # true = TUI po spuštění rovnou skenuje (jinak ukáže poslední uložený výsledek)
    scan_on_start: bool = False
    # jazyk rozhraní: auto = podle LANG / LC_ALL (cs_* → čeština, jinak angličtina)
    language: Literal["auto", "cs", "en"] = "auto"

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k not in _REMOVED_UI_KEYS}
        return data


class CIConfig(_Model):
    # Vlastníci akcí, které kontrola ci-security nemusí mít připnuté na SHA (např. tvůj účet).
    trusted_action_owners: list[str] = Field(default_factory=list)


MergeMethod = Literal["auto", "merge", "squash", "rebase"]


class PublishConfig(_Model):
    """Odeslání oprav: push větve, PR/MR a merge. Jen v globální konfiguraci."""

    # `auto` = první povolená metoda repa v pořadí merge → squash → rebase
    merge_method: MergeMethod = "auto"
    # remote pro push; prázdné = remote výchozí větve, jinak `origin`, jinak jediný remote
    remote: str | None = None

    @field_validator("remote")
    @classmethod
    def _remote_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.\-]*", value):
            raise ValueError(_("neplatný název remotu"))
        return value


class CleanupConfig(_Model):
    # Regenerovatelné složky (jen ignorované gitem), které nabízí úklid a kontrola disk-usage.
    dirs: list[str] = Field(
        default_factory=lambda: [
            "node_modules", ".venv", "venv", "target", "dist", "build", ".next", "__pycache__",
            ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", ".terraform",
        ]
    )  # fmt: skip
    inactive_days: int = Field(default=60, ge=1)
    min_mb: int = Field(default=200, ge=1)

    @field_validator("dirs")
    @classmethod
    def _plain_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name or "/" in name or name in (".", "..", ".git"):
                raise ValueError(
                    _("{name!r}: jen název složky (bez cesty, ne .git)").format(name=name)
                )
        return value


class NotifyConfig(_Model):
    """Kanály upozornění plánovaného skenu. Token ntfy jako u hostingů (env, příkaz, klíčenka)."""

    desktop: bool = True  # notify-send, jen když běží grafická relace
    ntfy_url: str | None = None  # https://ntfy.sh/moje-tema (http jen pro allow_http_hosts)
    token_source: TokenSource | None = None
    token_env: str | None = None
    token_cmd: str | None = None
    email: str | None = None  # jen přes lokální sendmail

    @property
    def name(self) -> str:
        return "notify-ntfy"  # klíč v klíčence

    @field_validator("ntfy_url")
    @classmethod
    def _ntfy(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if (
            not value.startswith(("https://", "http://"))
            or "@" in value.split("//", 1)[1].split("/", 1)[0]
        ):
            raise ValueError(_("ntfy_url musí být https:// URL tématu bez přihlašovacích údajů"))
        return value

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", value.strip()):
            raise ValueError(_("email musí být jedna adresa"))
        return value.strip()

    @model_validator(mode="after")
    def _source(self) -> NotifyConfig:
        if self.token_source is None:
            self.token_source = "env" if self.token_env else "cmd" if self.token_cmd else "none"
        return self


class ScheduleConfig(_Model):
    fetch: bool = False  # plánovaný sken s `git fetch` (jinak jen lokální stav)


# Zápisy, které MCP server smí nabídnout (každý se zapíná zvlášť; výchozí je jen čtení).
MCPWrite = Literal["fix_branch", "allowlist", "sync", "publish"]
MCP_WRITES: tuple[MCPWrite, ...] = ("fix_branch", "allowlist", "sync", "publish")


class MCPConfig(_Model):
    # false (výchozí) = MCP server skenuje jen offline; true = i síťové kontroly (OSV, hostingy)
    network: bool = False
    # true (výchozí) = nástroje vidí a skenují jen repa pod sledovanými složkami
    restrict_to_roots: bool = True
    # povolené zápisové nástroje; prázdné = server je jen pro čtení. Zápisy platí vždy
    # jen pro repa pod sledovanými složkami, i když je restrict_to_roots vypnuté.
    writes: list[MCPWrite] = Field(default_factory=list)


class UpdatesConfig(_Model):
    # true = TUI jednou denně zjistí na GitHub Releases, zda vyšla nová verze (ne v offline režimu)
    check: bool = True


class Config(_Model):
    roots: list[RootConfig] = Field(default_factory=list)
    forges: list[ForgeConfig] = Field(default_factory=list)
    keys: dict[str, str] = Field(default_factory=dict)
    checks: ChecksConfig = Field(default_factory=ChecksConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    allowlist: list[AllowEntry] = Field(default_factory=list)
    ignore_paths: list[str] = Field(default_factory=list)
    ignore_repos: list[str] = Field(default_factory=list)
    license: LicenseId = "MIT"
    templates_dir: str | None = None
    # false = nepřipojovat `-o BatchMode=yes` (pro wrapper v core.sshCommand, který argumenty nepředá)
    ssh_batch_mode: bool = True
    # Gitleaks hlásí i zjevně vymyšlené hodnoty (řady abcdef…/123456…, zkrácené příklady). Vestavěný
    # skener je zahazuje vždy; tady se stejný filtr použije i na nálezy gitleaksu (počet viz report).
    # Jen globálně – `.repo-doctor.toml` cizího repa si nálezy skrýt nemůže.
    hide_fake_secrets: bool = True
    # Hosty, pro které je povolený nešifrovaný http (např. Forgejo za Tailscale). Jen v globální
    # konfiguraci – `.repo-doctor.toml` cizího repa si http povolit nemůže.
    allow_http_hosts: list[str] = Field(default_factory=list)
    # Tvoje e-maily navíc ke `git config --global user.email` (kontrola email-exposure).
    my_emails: list[str] = Field(default_factory=list)
    ui: UIConfig = Field(default_factory=UIConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)

    @field_validator("allow_http_hosts")
    @classmethod
    def _http_hosts(cls, value: list[str]) -> list[str]:
        from repo_doctor.netpolicy import validate_entries

        return validate_entries(value)

    @model_validator(mode="after")
    def _unique_forges(self) -> Config:
        url = self.notify.ntfy_url
        if url and url.startswith("http://"):
            from repo_doctor.netpolicy import HttpHostPolicy

            if not HttpHostPolicy(self.allow_http_hosts).allows_http_url(url):
                raise ValueError(
                    _("notify.ntfy_url přes http:// je povolená jen pro host z allow_http_hosts")
                )
        names = [f.name for f in self.forges]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(
                _("duplicitní názvy hostingů: {names}").format(names=", ".join(sorted(dupes)))
            )
        return self

    def forge(self, name: str) -> ForgeConfig | None:
        return next((f for f in self.forges if f.name == name), None)


class RepoOverrides(_Model):
    """`.repo-doctor.toml` v repozitáři – přepisuje globální nastavení pro dané repo."""

    checks: ChecksConfig | None = None
    limits: dict[str, Any] | None = None
    allowlist: list[AllowEntry] = Field(default_factory=list)
    ignore_paths: list[str] = Field(default_factory=list)
    license: LicenseId | None = None


def format_validation_error(err: ValidationError, source: str) -> str:
    lines = [_("Neplatná konfigurace ({source}):").format(source=source)]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or _("(kořen)")
        msg = str(e["msg"]).removeprefix("Value error, ")
        lines.append(f"  • {loc}: {msg}")
    return "\n".join(lines)


def parse_config(data: dict[str, Any], source: str = "config.toml") -> Config:
    try:
        return Config.model_validate(data)
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, source)) from err


def load_repo_overrides(repo_path: Path) -> RepoOverrides | None:
    file = repo_path / ".repo-doctor.toml"
    if not file.is_file():
        return None
    try:
        data = tomlkit.parse(file.read_text("utf-8")).unwrap()
        return RepoOverrides.model_validate(data)
    except (ValidationError, tomlkit.exceptions.TOMLKitError, OSError) as err:
        raise ConfigError(_("Neplatný {file}: {err}").format(file=file, err=err)) from err


def effective_for_repo(config: Config, overrides: RepoOverrides | None) -> Config:
    if overrides is None:
        return config
    update: dict[str, Any] = {}
    if overrides.checks is not None:
        update["checks"] = overrides.checks
    if overrides.limits:
        merged = config.limits.model_dump() | overrides.limits
        update["limits"] = LimitsConfig.model_validate(merged)
    if overrides.allowlist:
        update["allowlist"] = [*config.allowlist, *overrides.allowlist]
    if overrides.ignore_paths:
        update["ignore_paths"] = [*config.ignore_paths, *overrides.ignore_paths]
    if overrides.license:
        update["license"] = overrides.license
    return config.model_copy(update=update)


DEFAULT_HEADER = N_(
    """\
# repo-doctor – konfigurace
# Dokumentace: README.md, sekce Konfigurace. Tokeny sem nepatří (použij token_env,
# token_cmd nebo token_source = "keyring").
"""
)


class ConfigStore:
    """Načítá a ukládá config.toml. Úpravy jdou přes tomlkit dokument, takže komentáře zůstanou."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.config_file()
        self.doc: TOMLDocument = tomlkit.document()
        self.config = Config()

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Config:
        if not self.exists:
            self.doc = tomlkit.parse(_(DEFAULT_HEADER))
            self.config = Config()
            return self.config
        try:
            text = self.path.read_text("utf-8")
            self.doc = tomlkit.parse(text)
        except (OSError, tomlkit.exceptions.TOMLKitError) as err:
            raise ConfigError(
                _("Nelze načíst {path}: {err}").format(path=self.path, err=err)
            ) from err
        self.config = parse_config(self.doc.unwrap(), str(self.path))
        return self.config

    # -- zápis -------------------------------------------------------------
    def _validate_doc(self) -> Config:
        return parse_config(self.doc.unwrap(), str(self.path))

    def save(self) -> Config:
        """Zvaliduje dokument a atomicky ho zapíše. Při chybě se nic nezapíše."""
        config = self._validate_doc()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".toml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(tomlkit.dumps(self.doc))
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.config = config
        return config

    def _aot(self, key: str) -> AoT:
        if key not in self.doc:
            self.doc[key] = tomlkit.aot()
        item = self.doc[key]
        if not isinstance(item, AoT):
            raise ConfigError(_("`{key}` musí být pole tabulek ([[{key}]])").format(key=key))
        return item

    def _table(self, key: str) -> Table:
        if key not in self.doc:
            self.doc[key] = tomlkit.table()
        item = self.doc[key]
        if not isinstance(item, Table):
            raise ConfigError(_("`{key}` musí být tabulka ([{key}])").format(key=key))
        return item

    def _transaction(self, mutate: Any) -> Config:
        backup = tomlkit.dumps(self.doc)
        try:
            mutate()
            return self.save()
        except Exception:
            self.doc = tomlkit.parse(backup)
            raise

    @staticmethod
    def _clean(values: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in values.items() if v is not None}

    # roots
    def upsert_root(self, root: RootConfig, index: int | None = None) -> Config:
        def mutate() -> None:
            aot = self._aot("roots")
            data = self._clean(root.model_dump(exclude_defaults=True) | {"path": root.path})
            if index is None:
                aot.append(tomlkit.item(data))
            else:
                table = aot[index]
                for key in list(table.keys()):
                    if key not in data:
                        del table[key]
                for key, value in data.items():
                    table[key] = value

        return self._transaction(mutate)

    def remove_root(self, index: int) -> Config:
        def mutate() -> None:
            aot = self._aot("roots")
            del aot[index]
            if len(aot) == 0:
                del self.doc["roots"]

        return self._transaction(mutate)

    # forges
    def upsert_forge(self, forge: ForgeConfig, index: int | None = None) -> Config:
        def mutate() -> None:
            aot = self._aot("forges")
            data = self._clean(
                forge.model_dump(exclude_defaults=True) | {"name": forge.name, "type": forge.type}
            )
            if index is None:
                aot.append(tomlkit.item(data))
            else:
                table = aot[index]
                for key in list(table.keys()):
                    if key not in data:
                        del table[key]
                for key, value in data.items():
                    table[key] = value

        return self._transaction(mutate)

    def remove_forge(self, index: int) -> Config:
        def mutate() -> None:
            aot = self._aot("forges")
            del aot[index]
            if len(aot) == 0:
                del self.doc["forges"]

        return self._transaction(mutate)

    # obecné sekce
    def set_value(self, section: str, key: str, value: Any) -> Config:
        def mutate() -> None:
            table = self._table(section)
            if value is None:
                if key in table:
                    del table[key]
            else:
                table[key] = value

        return self._transaction(mutate)

    def set_top(self, key: str, value: Any) -> Config:
        def mutate() -> None:
            if value is None:
                if key in self.doc:
                    del self.doc[key]
            else:
                self.doc[key] = value

        return self._transaction(mutate)

    def add_allow(self, entry: AllowEntry) -> Config:
        def mutate() -> None:
            aot = self._aot("allowlist")
            aot.append(tomlkit.item(self._clean(entry.model_dump())))

        return self._transaction(mutate)

    def remove_allow(self, fingerprint: str) -> Config:
        def mutate() -> None:
            aot = self._aot("allowlist")
            for i in reversed(range(len(aot))):
                if aot[i].get("hash") == fingerprint:
                    del aot[i]
            if len(aot) == 0:
                del self.doc["allowlist"]

        return self._transaction(mutate)
