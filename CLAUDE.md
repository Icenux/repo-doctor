# CLAUDE.md – jak pracovat s repo-doctorem

## Spuštění

```sh
uv sync                       # závislosti (Python 3.12+)
uv run repo-doctor            # TUI (na terminálu)
uv run repo-doctor scan ~/projekty --offline --report md
uv run repo-doctor explain secrets-history
```

## Testy a kvalita

```sh
uv run pytest                 # testy nikdy nevolají síť (respx), repa se staví v tmp_path
uv run pytest --cov           # pokrytí ≥ 85 %
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                   # --strict
uv run pytest tests/test_snapshots.py --snapshot-update   # po vědomé změně vzhledu TUI
uv run python -m repo_doctor.reports.jsonreport > docs/report-schema.json  # po změně JSON schématu
uv run python -m repo_doctor.i18n_tools          # po změně textů: doplní locale/en.po (pak přelož)
```

Testovací „tajemství“ se skládají až za běhu (`tests/factory.py`), do repa nepatří žádné
skutečné ani regexům odpovídající hodnoty. Testy TUI používají `tests/tui_helpers.py`
(`make_app`, `FakeScanner`), ukázková data jsou v `tests/sample.py`.

## Zásada: funguje hned po instalaci

repo-doctor se nainstaluje z balíčku a musí fungovat bez dalších programů – **stačí `git`**.
Nic, co by uživatel musel doinstalovat (`uv`, `npm`, `pip`, `cargo`, `go`…), nesmí být podmínkou
funkce a repo-doctor je nespouští. Opravy píšeme v Pythonu (např. `lockfiles/` přepisuje lockfily
z metadat registrů). Volitelné programy (`gitleaks`, `notify-send`…) mají záložní chování a jsou
vyjmenované v `tests/test_no_external_tools.py`, který tuhle zásadu hlídá. `uv` je jen vývojový
nástroj. Nejde-li něco bezpečně udělat bez cizího nástroje, oprava se nenabídne a zůstane
ruční postup v dokumentaci kontroly – nikdy „spusť si X“ jako náhrada léčby.

## Architektura

- Jádro je oddělené od UI: `discovery`, `gitwrap`, `checks/`, `scanner`, `fixes`, `reports/`,
  `forges/`, `config`, `netpolicy` (http jen pro `allow_http_hosts`), `cleanup` (koš),
  `sync` (jen ff), `lockfiles` (přepis `uv.lock` / `package-lock.json` čistě v Pythonu), `publish` (push, PR/MR, merge), `guard`, `sbom`, `notify`, `schedule`, `mcpserver`. TUI (`tui/`), CLI
  (`cli.py`) i MCP server jsou tenké vrstvy nad ním.
- Co mění soubory, repa nebo hosting, je volitelné, s náhledem a potvrzením (výchozí „Zrušit“).
  Léčba jen vytváří větev; push, PR, merge a lokální merge jsou samostatné kroky v `publish`
  (jen opravná větev, nikdy force, merge na hostingu jen po zelené CI). MCP server je jen pro čtení.
- `gitwrap.Git` je jediné místo, kde se volá `git` (timeouty, bezpečné prostředí, redakce chyb).
- Kontroly nevolají síť: scanner předem připraví `RepoContext.deps` (OSV, registry, licence),
  `RepoContext.forge_snapshot` (API hostingu), `noreply_email` a `action_shas`.
- Zkratky jsou jen v `keymap.py` (TUI, nápověda i README se generují odtud; test hlídá tabulky v README).
- Texty pro uživatele jsou česky v `_("…")` (`repo_doctor.i18n`), anglicky v `locale/en.po`.
  Proměnné přes `_("… {jmeno} …").format(jmeno=…)`, ne f-string; tvary podle čísla přes
  `czech.plural`. Test hlídá úplnost překladu (#33: fáze 1 TUI, CLI, zkratky; fáze 2 kontroly,
  opravy, dokumentace kontrol; fáze 3 reporty, šablony a zbytek jádra). Texty v konstantách modulů a atributech tříd přes `N_()`,
  překlad `_()` až při použití.

## Přidání nové kontroly

1. Vytvoř soubor v `src/repo_doctor/checks/` (moduly se načítají automaticky):

   ```python
   from repo_doctor.checks.base import Check, RepoContext, register
   from repo_doctor.i18n import N_, _
   from repo_doctor.models import Category, Finding, Severity

   @register
   class MyCheck(Check):
       id = "my-check"
       title = N_("Krátký název")
       severity = Severity.LOW
       category = Category.MAINTENANCE
       # network = True / needs_forge = True, pokud potřebuje síťová data

       def run(self, repo: RepoContext) -> list[Finding]:
           return [self.finding(_("zpráva"), path="soubor", key="stabilní-klíč")]
   ```

   `key` je povinný a nezávislý na jazyku (z něj je otisk pro allowlist). Nedostupná data →
   `raise SkipCheck(_("důvod"))`. Tajemství nikdy do `Finding` nedávej –
   jen `masking.mask(...)`.
2. Volitelně `fix(repo, findings) -> Patch` (jen vrací změny, nic nezapisuje) a `fixable = True`.
3. Napiš `docs/checks/<id>.md` (sekce „Co kontrola hledá“, „Proč to vadí“, „Postup“)
   a anglicky `docs/checks/en/<id>.md` („What the check looks for“, „Why it matters“, „Steps“) –
   test `test_every_check_is_documented` to vyžaduje.
4. Testy: pozitivní i negativní případ přes `tests/factory.py`.

## Vydání

Uživatelé instalují sestavenou aplikaci (balíčky), ne přes uv/pipx – uv je jen vývojový nástroj.

```sh
uv sync --locked --no-dev --group build && uv run packaging/build.sh   # dist/repo-doctor
packaging/package.sh        # dist/release: .deb, .rpm, tarball, PKGBUILD, SHA256SUMS (potřebuje nfpm)
```

Vydání: zvedni verzi v `pyproject.toml` a `src/repo_doctor/__init__.py`, doplň CHANGELOG
a pushni tag `X.Y.Z` (bez `v`) – `.github/workflows/release.yml` sestaví, otestuje instalaci na
distribucích, vytvoří GitHub Release a nasadí APT/RPM repozitář na Pages (secret
`RELEASE_SIGNING_KEY`).

## Git workflow

Conventional Commits, po každém bloku musí projít testy. Rozhodnutí zapisuj do
`docs/DECISIONS.md`.
