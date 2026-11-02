# deps-vulnerable – Zranitelné závislosti

**Kategorie:** Bezpečnost · **Severity:** HIGH (podle závažnosti z OSV) · **Automatická oprava:** ano (`uv.lock`, `package-lock.json`)

## Co kontrola hledá

Parsuje lockfily (`uv.lock`, `poetry.lock`, `requirements*.txt` s `==`, `pyproject.toml` s pinem, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `go.sum`) a dávkově se ptá **OSV.dev** (`/v1/querybatch`, bez klíče). Závažnost bere z databáze (GHSA: CRITICAL/HIGH → HIGH, MODERATE → MED, LOW → LOW). V offline režimu je přeskočena. Výsledky se cachují.

## Automatická oprava

V léčbě (`f`) repo-doctor aktualizuje zranitelné závislosti na **nejnižší verzi, která je opravená** (podle OSV), a výsledný lockfile uloží do nové větve – nemusíš spouštět žádné další CLI. Správce balíčků (`uv lock --upgrade-package`, `npm update --package-lock-only --ignore-scripts`) běží v dočasné složce jen s manifestem a lockfilem z HEAD, takže repo ani pracovní strom se nemění a nic se nespouští z repa. Nabídne se jen když je `uv`, resp. `npm` v `PATH` a OSV opravenou verzi zná. Co omezení v manifestu nepustí (např. `requests<2.20`), zůstane a oprava na to upozorní. `Cargo.lock`, `go.sum`, `yarn.lock` a `pnpm-lock.yaml` zatím ručně. Po mergi spusť testy projektu.

## Proč to vadí

Známé zranitelnosti mají veřejné exploity; aktualizace bývá nejlevnější obrana.

## Postup

1. Podívej se na ID zranitelnosti (GHSA/CVE/PYSEC) a zjisti opravenou verzi.
2. Aktualizuj závislost – v repo-doctoru přes léčbu (`f`), jinde ručně (`uv lock --upgrade-package X`, `npm update X`, `cargo update -p X`, `go get X@latest`).
3. Pokud aktualizace nejde, zvaž dopad a přidej nález do allowlistu s důvodem.
