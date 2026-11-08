# Návrh: my-repo – správa repozitářů (0.5.0, #81)

Z repo-doctoru se stává **my-repo**. Hlavní funkcí je přehled o stavu repozitářů a práce s nimi
(soubory, commity, větve, issues, PR, Actions, vydání). Léčba a nálezy zůstávají jako jedna
z funkcí, ne jako účel aplikace.

## Rozsah 0.5.0

- Prohlížeč repa: strom, náhled souboru, commity, diff.
- Editace souborů.
- Issues a pull requesty na hostingu.
- Actions / CI.
- Větve a tagy, vydání (releases).
- Hostingy: GitHub, Forgejo/Gitea, GitLab. Lokální klony i repa jen na hostingu.

**Mimo rozsah:** nastavení repa (ochrana větví, secrets, webhooky), zakládání a mazání repozitářů,
vestavěný editor v TUI.

## Rozhodnutí

| Téma | Rozhodnutí |
|---|---|
| Identita | my-repo; přehled a práce s repy je hlavní funkce, léčba jedna z nich |
| Přejmenování | Celé najednou. Příkaz `repo-doctor` zůstane dočasně jako alias, konfigurace a data se přenesou automaticky, balíčky nový nahradí starý |
| Menu | Levé menu podle funkcí: Přehled, Repa, Issues, Pull requesty, Actions, Léčba, Nastavení; platí přes všechna repa |
| Obrazovka repa | Enter na repu otevře záložky: Soubory, Commity, Větve a tagy, Issues, PR, Actions, Vydání, Léčba |
| Vzdálená repa | Čtení i editace přes API hostingu u repozitářů bez klonu |
| Editace lokálně | Přes `$VISUAL` / `$EDITOR`, pak diff a commit na pracovní větev |
| Editace přes API | Vždy na novou větev, volitelně rovnou PR |
| Zásady zápisů | Náhled a potvrzení s výchozí volbou „Zrušit“, nikdy force, merge jen po zelené CI, do výchozí a chráněné větve se přímo nezapisuje |
| Žádné cizí nástroje | Zůstává zásada „stačí git“; HTTP přes stávající `httpclient` a `netpolicy` |

## Architektura

```
              TUI (tui/)      CLI (cli.py)      MCP (mcpserver.py)
                   \               |               /
                    +---- jádro správy (nové) ----+
                    |  jednotné rozhraní repa:    |
                    |  strom, soubor, commity,    |
                    |  větve, tagy, porovnání     |
                    +--------+----------+---------+
                             |          |
                     gitwrap.Git     forges/ (API)
                     (lokální klon)  GitHub, Forgejo/Gitea, GitLab
```

- Jádro zůstává oddělené od UI, TUI, CLI i MCP jsou tenké vrstvy nad ním.
- `gitwrap.Git` zůstává jediným místem, kde se volá `git`.
- Jednotné rozhraní repa má dvě implementace (lokální klon, hosting). UI nerozlišuje, odkud data jsou.
- Zápisy jdou přes společný potvrzovací dialog a audit log (viz bezpečnostní model).

## Menu a obrazovky

```
┌─ my-repo ──────────────────────────────────────┐
│ ▸ Přehled    │ Stav všech repozitářů            │
│   Repa       │ repo        větev  ↑↓  PR  CI    │
│   Issues     │ api-server  main   0/0  2  ✓     │
│   Pull req.  │ web         dev    1/3  0  ✗     │
│   Actions    │ docs        main   0/0  1  ✓     │
│   Léčba      │                                  │
│   Nastavení  │ Enter na repu → záložky:         │
│              │ Soubory Commity Větve Issues PR  │
│              │ Actions Vydání Léčba             │
└──────────────┴──────────────────────────────────┘
```

Stávající triage nálezů a léčba se přesouvají do sekce a záložky „Léčba“ beze ztráty funkcí.
Zkratky zůstávají jen v `keymap.py`.

## Bezpečnostní model zápisů

- Každý zápis (větev, tag, issue, PR, vydání, editace) má náhled a potvrzení, výchozí je „Zrušit“.
- Žádný force push. Merge na hostingu jen po zelené CI.
- Smazat jde jen vzdálená větev, která není výchozí ani chráněná.
- Zápis přes API a lokální editace míří na novou nebo pracovní větev.
- Každý zápis se zapíše do audit logu (co, kde, kdy, přes TUI / CLI / MCP).
- MCP: zápisy vypnuté ve výchozím stavu, zapínají se po jednom v Nastavení (navazuje na #96).

## Rozkouskování (milestone 0.5.0)

| # | Issue | Po |
|---|---|---|
| #110 | Přejmenování na my-repo | – |
| #111 | Jádro: jednotné rozhraní nad klonem a hostingem | #110 |
| #122 | Bezpečnostní model zápisů | #110 (časně) |
| #112 | Nové menu a kostra TUI | #111 |
| #113 | Přehled stavu repozitářů | #112 |
| #114 | Prohlížeč souborů a commitů | #112 |
| #115 | Větve a tagy | #112, #122 |
| #116 | Issues | #112 |
| #117 | Pull requesty | #112 |
| #118 | Actions a CI | #112 |
| #119 | Vydání | #115 |
| #120 | Editace lokálních souborů přes `$EDITOR` | #114, #122 |
| #121 | Editace přes API | #111, #114, #122 |
| #123 | MCP: nové nástroje a úrovně oprávnění (#96) | čtecí funkce |
| #124 | Dokumentace, nápověda, zkratky, překlady | průběžně |

## Dořešená rozhodnutí

### Editor, když žádný není (#120)

Pořadí hledání: volba `editor` v nastavení my-repo, `core.editor` z gitu, `$VISUAL`, `$EDITOR`, `editor`
(Debian/Ubuntu alternativy), `sensible-editor`, `vi`, `nano`. První nalezený se použije. Nenajde-li se nic,
zobrazí se hláška s návodem nastavit `editor` v Nastavení nebo `$EDITOR`. Nic se nespouští, neinstaluje
a neradí „nainstaluj X“. Vestavěný editor se nedělá.

### Audit log (#122)

- Soubor `~/.local/state/my-repo/audit.jsonl` (XDG state; respektuje `XDG_STATE_HOME`), práva `600`.
- Jeden řádek JSON na každý zápis: čas, rozhraní (`tui` / `cli` / `mcp`, u MCP i jméno klienta), akce, repo,
  cíl (větev, číslo issue nebo PR, tag), výsledek (`ok` / `zrušeno` / `chyba`).
- Nikdy se nezapisuje obsah souborů, diffy, texty komentářů ani tajemství; e-maily se maskují.
- Rotace při 1 MB, uchová se 5 souborů. Prohlížeč v TUI (sekce Nastavení) a příkaz `my-repo audit`.
- Zápis do logu nesmí shodit samotný zápis: selže-li, ukáže se varování.

### Rozdíly hostingů (#111, #118, #121)

- Každý hosting deklaruje své **schopnosti** (např. `read_tree`, `edit_files`, `ci_logs`, `ci_rerun`,
  `ci_dispatch`, `releases`). Verze Forgejo/Gitea, které Actions nemají, se pozná podle odpovědi API.
- Chybějící schopnost UI skryje nebo zašedí s vysvětlením, nikdy nespadne.
- GitHub, Forgejo/Gitea i GitLab mají ve 0.5.0 **stejný rozsah**, včetně editace přes API a logů CI.
  GitLab pipelines se zobrazují jako Actions.
- Rozdíly (SHA souboru, konflikty, názvy polí) řeší adaptér hostingu v `forges/`, ne UI. Každý je pokrytý
  testy s `respx`.
