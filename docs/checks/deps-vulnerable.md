# deps-vulnerable – Zranitelné závislosti

**Kategorie:** Bezpečnost · **Severity:** HIGH (podle závažnosti z OSV) · **Automatická oprava:** ano (`uv.lock`, `package-lock.json`)

## Co kontrola hledá

Parsuje lockfily (`uv.lock`, `poetry.lock`, `requirements*.txt` s `==`, `pyproject.toml` s pinem, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `go.sum`) a dávkově se ptá **OSV.dev** (`/v1/querybatch`, bez klíče). Závažnost bere z databáze (GHSA: CRITICAL/HIGH → HIGH, MODERATE → MED, LOW → LOW). V offline režimu je přeskočena. Výsledky se cachují.

## Automatická oprava

V léčbě (`f`) repo-doctor přepíše zranitelnou závislost v lockfilu na **nejnižší opravenou verzi** (podle OSV) a výsledek uloží do nové větve. Nepotřebuješ `uv`, `npm` ani žádný další program: verzi, odkaz a kontrolní součet bere sám z PyPI a npm registru a lockfile upraví čistě v Pythonu. Podporuje `uv.lock` a `package-lock.json` (lockfileVersion 3).

Přepis je záměrně opatrný, mění jen verzi jednoho balíčku. Nabídka se **neobjeví** (a zůstává ruční postup níže), když by bylo potřeba znovu řešit strom závislostí: nová verze má jiné závislosti, jiný balíček její verzi zakazuje (např. `idna<3.5`), balíček nepochází z veřejného registru, lockfile má neobvyklé formátování nebo omezení prostředí, nebo opravená verze mění instalační skripty. U `uv.lock` se navíc výstup ověřuje proti staré verzi téhož balíčku, takže se nezapíše nic, co neumíme přesně zreprodukovat. Po mergi spusť testy projektu. `Cargo.lock`, `go.sum`, `yarn.lock`, `pnpm-lock.yaml` a `poetry.lock` zatím ručně.

## Proč to vadí

Známé zranitelnosti mají veřejné exploity; aktualizace bývá nejlevnější obrana.

## Postup

1. Podívej se na ID zranitelnosti (GHSA/CVE/PYSEC) a zjisti opravenou verzi.
2. Aktualizuj závislost. U `uv.lock` a `package-lock.json` to udělá léčba (`f`), u ostatních a tam, kde léčba nepomůže, ručně (`uv lock --upgrade-package X`, `npm update X`, `cargo update -p X`, `go get X@latest`).
3. Pokud aktualizace nejde, zvaž dopad a přidej nález do allowlistu s důvodem.
