# secrets-tree – Tajemství v aktuálních souborech

**Kategorie:** Bezpečnost · **Severity:** HIGH · **Automatická oprava:** ne

## Co kontrola hledá

Prochází sledované i nesledované (neignorované) soubory a hledá API klíče (AWS, GitHub, GitLab, Slack, OpenAI, Anthropic), privátní SSH/PGP klíče, JWT, connection stringy s heslem a obecná tajemství s vysokou entropií. Pokud je nainstalovaný `gitleaks`, použije se jeho detekce. Lockfily a testovací fixtury (`fixtures/`, `testdata/`, `__snapshots__/`) se přeskakují, stejně jako řádky označené `gitleaks:allow` nebo `repo-doctor:allow`. Zjevně vymyšlené hodnoty (`abcdef…`, `123456…`, `9f8e7d6c5b4a`, zkrácené `abc...xyz`) se nehlásí ani u nálezů z `gitleaks`; jejich počet je v reportu (`Skryto jako zjevně vymyšlené hodnoty`) a filtr jde vypnout volbou `hide_fake_secrets = false` v `config.toml`. Skutečné slabé heslo jako `Company123456` se hlásí dál.

## Proč to vadí

Tajemství ve verzovaném souboru se dostane do každého klonu, zálohy a forku. Jakmile je jednou commitnuté, zůstává v historii i po smazání.

## Postup

1. Klíč ihned zneplatni a vygeneruj nový – rotace je nutná vždy, i když repo není veřejné.
2. Přesuň hodnotu do proměnné prostředí, správce hesel (`pass`) nebo souboru mimo repo.
3. Soubor přidej do `.gitignore`; pokud už je commitnutý, řeš i kontrolu `secrets-history`.
4. Pokud jde o falešný poplach, přidej nález do allowlistu (`a` v kartě repa) s důvodem.
