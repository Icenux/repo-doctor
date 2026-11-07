# secrets-history – Tajemství v historii commitů

**Kategorie:** Bezpečnost · **Severity:** HIGH · **Automatická oprava:** ne

## Co kontrola hledá

Projde celou historii (`git log --all -p`) a skenuje přidané řádky stejnými pravidly jako `secrets-tree`. Nález ukazuje commit, který tajemství přidal. U obřích repozitářů sken skončí po časovém limitu (`limits.history_timeout_s`) s nálezem `scan-incomplete`. Zjevně vymyšlené hodnoty se skrývají stejně jako u `secrets-tree` (volba `hide_fake_secrets`).

## Proč to vadí

Klíč zůstává v historii i po smazání souboru. Kdo repo naklonuje, klíč získá. Automaticky opravit to nejde – přepis historie je vždy tvoje rozhodnutí.

## Postup

1. Klíč ihned zneplatni a vygeneruj nový (rotace vždy).
2. Odstraň ho z historie: `git filter-repo --path config/prod.env --invert-paths` (nebo `--replace-text`).
3. Force push a dej vědět případným spolupracovníkům (musí naklonovat znovu).
4. Přidej soubor do `.gitignore` (oprava `gitignore-incomplete` / `env-committed` to připraví).

## Poznámka

Automaticky neopravitelné: přepis historie je vždy tvoje rozhodnutí.
