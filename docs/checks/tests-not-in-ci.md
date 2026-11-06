# tests-not-in-ci – Testy existují, ale CI je nespouští

**Kategorie:** Údržba · **Severity:** LOW · **Automatická oprava:** ne

## Co kontrola hledá

Platí, když repo má testy i CI konfiguraci (`.github/workflows`, `.forgejo/workflows`, `.gitea/workflows`, `.gitlab-ci.yml`, `.woodpecker*`, `.circleci`, `Jenkinsfile`, `.travis.yml`…), ale v žádném z CI souborů není příkaz, který testy spouští (`pytest`, `tox`, `npm test`, `cargo test`, `go test`, `make test`, `gradle`, `mvn` a podobně). CI soubory skládané z jiných (`uses: ./…`, `include:`, `extends:`) nebo nečitelné se nehádají.

## Proč to vadí

Testy, které CI nespouští, časem přestanou procházet a nikdo si toho nevšimne. Zelený odznak v repu pak jen tvrdí, že je vše v pořádku.

## Postup

1. Přidej krok s testy do CI konfigurace (např. `pytest`, `npm test`).
2. Používáš-li vlastní skript nebo nástroj, který tento nález nepozná, dej nález do allowlistu.
3. Ověř, že CI opravdu běží a je zelená (kontrola `forge-ci-failing`).
