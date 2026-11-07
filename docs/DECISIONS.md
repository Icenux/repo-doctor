# Rozhodnutí (ADR v kostce)

Záznam rozhodnutí, která zadání nechalo otevřená. Nejnovější nahoře v rámci sekce.

## Git a bezpečnost

- **Opravy přes dočasný index, ne `git switch`.** Commity oprav se staví v dočasném
  indexu (`GIT_INDEX_FILE`) pomocí `hash-object` → `update-index` → `write-tree` →
  `commit-tree` a nakonec `git branch repo-doctor/fixes-<datum> <commit>`. Pracovní strom,
  index, aktuální branch ani HEAD se nikdy nezmění – uživatel si větev zkontroluje sám.
  `git rm --cached` u citlivých souborů se tak projeví jen ve větvi oprav, soubor na disku
  zůstává. Existující větev se nikdy nepřepíše (`-2`, `-3`… přípona).
- **Opravy odmítnou repo s jakoukoli změnou**, včetně nesledovaných souborů – je to
  nejbezpečnější výklad „nesahá na necommitnuté změny“.
- **Commity oprav** se vytváří s `--no-gpg-sign` a bez hooků (commit-tree je nespouští):
  podpisový pinentry by v TUI zamrzl. Uživatel může větev před mergem přepodepsat.
- **Git bez vedlejších efektů:** všechny příkazy běží s `GIT_OPTIONAL_LOCKS=0` (status
  nepřepisuje index), `core.fsmonitor=false`, prázdným `diff.external`, `--no-textconv`
  a `--no-ext-diff` (cizí `.git/config` nesmí spouštět programy), `GIT_TERMINAL_PROMPT=0`
  a SSH v `BatchMode`. Uživatelské cesty/refy jdou vždy za `--`/`--end-of-options`,
  názvy větví se ověřují `git check-ref-format --branch`.
- **SSH příkaz (0.1.2):** git dostává vždy `GIT_SSH_COMMAND` (proměnná má přednost před
  `core.sshCommand`, takže lokální `core.sshCommand` cizího repa se nikdy nespustí – hardening
  ho navíc přebíjí prázdnou hodnotou). Základ se bere v pořadí: proměnná `GIT_SSH_COMMAND`
  z prostředí → globální/systémový `core.sshCommand` (`git config --global/--system`,
  spuštěno v `$HOME`, timeout 5 s, výsledek cachovaný na běh) → `ssh`. Na konec se přidá
  `-o BatchMode=yes`, aby se SSH nikdy neptal na heslo a nezasekl TUI. Wrapper skript proto
  musí zbylé argumenty předat dál (`exec ssh "$@"`); pokud to neumí, vypne se přidávání
  volbou `ssh_batch_mode = false` v konfiguraci (pak odpovědnost za neinteraktivní běh
  nese wrapper).
- **České hlášky SSH chyb (0.1.2):** `sshhelp.explain_ssh_error(stderr, url)` je čistá
  funkce; rozpozná neznámý klíč serveru, odmítnutý klíč a nedostupný server, jinak vrátí
  None a zobrazí se původní (redigovaný) text. Ve výzvě `ssh -p <port> git@<host>` se `-p`
  vynechá, když URL port nemá (výchozí 22); uživatel se bere jen z SSH URL a jen když je
  „bezpečný“ (u https je v userinfo typicky token), jinak `git`. Původní text gitu zůstává
  vidět: u klonu v notifikaci „(git: …)“, u `--fetch` v `errors.fetch_detail` (reporty, karta
  repa). „Test připojení“ hostingu je API přes HTTPS – při `clone_protocol = "ssh"` proto
  navíc zkusí `git ls-remote --heads` na SSH URL prvního repa (jen čte) a problém ukáže
  jako varování.
- **Hooky a příkazy z cizího repa (0.1.4):**
  - **Hooky** vypíná `-c core.hooksPath=/dev/null` u každého volání (`_SAFE_CONFIG`, i clone
    a ls-remote). `/dev/null` není adresář, takže git žádný hook nenajde; ověřeno na gitu
    2.43 testem (hook `reference-transaction` z `.git/hooks` i z lokálního `core.hooksPath`
    by jinak zablokoval `git branch`). Vypnou se i globální hooky uživatele – repo-doctor
    commituje jen do vlastní větve oprav a hooky nepotřebuje.
  - **`remote.*.uploadpack`/`receivepack` nejde přebít přes `-c`**: git bere *první* hodnotu
    („more than one uploadpack given, using the first“), tedy tu z lokální konfigurace. Proto
    fetch, ls-remote i clone předávají `--upload-pack=git-upload-pack`. `receivepack` se
    nepoužívá – repo-doctor nikdy nepushuje.
  - **`core.gitProxy`** platí také „první vyhrává“; vypíná ho prázdné `GIT_PROXY_COMMAND`
    (git pak konfiguraci vůbec nečte). Vypne to i `core.gitProxy` uživatele – `git://` je
    stejně zakázaný.
  - **Credential helpery:** pokud lokální konfigurace definuje `credential.helper` nebo
    `credential.<url>.helper`, přidá se `-c credential.helper=` (v gitu prázdná hodnota
    vyprázdní seznam, tedy i helpery pro URL) a za něj znovu helpery ze systémové a globální
    konfigurace uživatele (`git config --system/--global --includes --get-regexp`, cache na
    běh). Uživatelův helper tak funguje dál; helper z `includeIf` podle složky se mimo repo
    nevyhodnotí, stejně jako u `core.sshCommand`.
  - **Navíc oproti zadání:** lokální `core.alternateRefsCommand` se při fetchi s alternates
    opravdu spustí (ověřeno) → přebíjí se prázdnou hodnotou. Fetch běží s
    `--no-recurse-submodules` (submodul má vlastní, neošetřenou konfiguraci). Lokální
    `http.sslVerify=false` (i pro konkrétní URL) se přebíjí na `true`, `http.proxy`
    a `remote.*.proxy` na prázdnou hodnotu – jinak by šlo s globálním credential helperem
    uživatele odposlechnout přihlašovací údaje.
  - **Protokoly:** `GIT_ALLOW_PROTOCOL=https:ssh` (má přednost i před lokálním
    `protocol.*.allow`). `git://` je nešifrovaný a neověřený a jen u něj se uplatní
    `core.gitProxy`; `http://` posílá přihlášení v otevřené podobě; `file`/lokální cesta by
    spustila `upload-pack` lokálně a `ext::` libovolný příkaz. Odmítnutí se překládá na
    „Remote používá nepovolený protokol X…“. Testy si `file` povolují interním přepínačem
    `_allow_protocols` (fixture `allow_file`), veřejná volba neexistuje.
- **`http://` jen pro hosty z `allow_http_hosts` (0.2.0):**
  - **Per-host, ne globální přepínač:** výchozí stav (prázdný seznam) se chová jako 0.1.4.
    Uživatel povolí jen konkrétní důvěryhodné hosty (Forgejo v Tailscale). Volba je jen
    v globálním `config.toml` – `RepoOverrides` ji nezná (`extra="forbid"`), takže cizí repo
    si `http` nepovolí.
  - **Per-operace:** `GIT_ALLOW_PROTOCOL` se o `http` rozšíří jen pro jeden příkaz, a jen když
    všechny `http://` URL, na které se může připojit, mají povolený host. U fetch se berou
    všechny remoty (i víc URL jednoho remotu a `branch.*.remote` zadané jako URL), vždy po
    přepisu `insteadOf` (`git ls-remote --get-url`) – jinak by lokální konfigurace cizího repa
    přesměrovala povolenou URL jinam. URL přes https/ssh seznam nepotřebují (zůstávají
    šifrované); proto jeden `https` remote na GitHub `http` pro Tailscale remote neblokuje.
  - **Přesměrování:** s povoleným `http` běží příkaz s `http.followRedirects=false` –
    `GIT_ALLOW_PROTOCOL` hlídá jen schéma, ne host, takže `initial` by pustil přesměrování
    z povoleného hosta na libovolný jiný http host. Lokální `http.followRedirects`
    (i pro konkrétní URL) hardening přebíjí na `false`.
  - **Porovnání hosta:** malá písmena, bez koncové tečky, IDN → punycode; IP adresy přes
    `ipaddress`. Nestandardní číselné zápisy (`100.1`, `0x64.1.1.1`) se neuznají ani jako
    hostname, ani jako IP – curl by je převedl jinak, než vypadají.
  - **API hostingů:** `HttpClient` odmítne každý `http://` požadavek (i po přesměrování) na host
    mimo seznam ještě před odesláním (`InsecureHostError`) – token tak nikdy neodejde po http
    jinam. Hosting s `http://` URL mimo seznam je „nedostupný“ s vysvětlením (načtení
    konfigurace kvůli tomu neselže, aby šlo seznam doplnit v Nastavení).
- **`--fetch`** je jediná výjimka z „CLI nic nemění“: aktualizuje jen remote-tracking refy
  a je vypnutý ve výchozím stavu (zadání: „žádný fetch bez `--fetch`“).
- **Tajemství se v modelu vůbec nevyskytují.** Skener vrací jen maskovanou ukázku
  (`AKIA…(20 znaků)`) a sha256 otisk; surová hodnota modul `secrets_scan` neopustí.
  Tokeny hostingů jsou v objektu `Token` (repr maskovaný, nepicklovatelný) a jsou
  registrované v globálním redakčním registru, přes který jdou logy, chybové hlášky
  gitu a výsledné reporty (obrana do hloubky).
- **Otisk tajemství** je `scrypt(secret, salt=„repo-doctor/secret-fingerprint/v2“, n=2¹⁴)`,
  zkrácený na 16 hex znaků. Sůl je pevná (ne náhodná), protože otisk musí být stejný na všech
  strojích – allowlist v `.repo-doctor.toml` se commituje a používá v CI. Pomalá funkce brání
  slovníkovému útoku na sdílený report; po změně verze je potřeba allowlist přegenerovat.
- **`token_cmd`** se spouští bez shellu (`shlex.split`), s timeoutem 15 s, stdin
  `/dev/null`; bere se první neprázdný řádek (jako `pass show`). Výstup se nikdy
  nepropisuje do chyb – ani při nenulovém exit kódu. Běží ve vlastní skupině procesů, po
  timeoutu se ukončí celá skupina (i potomci, kteří by drželi rouru).
- **Cizí vlastník / `safe.directory` (0.1.2):** scanner nejdřív spustí jediné
  `git rev-parse --git-dir` (bez čtení lokální konfigurace). Když git odpoví „detected dubious
  ownership“, repo se označí `untrusted_owner`, žádný další příkaz gitu se nespustí, jeho
  `.repo-doctor.toml` se nečte a vznikne jediný nález `unsafe-ownership` s postupem
  `git config --global --add safe.directory <cesta>`. repo-doctor `safe.directory` nikdy
  nenastavuje ani nepřebíjí přes `-c` – rozhodnutí důvěřovat cizímu repu patří uživateli.
  **Severity LOW** (ne „info“ – žádnou takovou úroveň nemáme a nález je potřeba vidět, ale
  repo samo o sobě není v nebezpečí; CLI tak skončí s 1 jen při `--fail-on low`).
  **Skóre se nepočítá** (`score = null` v JSON, `–` v TUI a reportech), repo je v triáži ve
  skupině NELZE ZKONTROLOVAT na konci a do průměrného zdraví ani historie se nezapočítá.
- **Skóre:** 100 − 25 za HIGH, 9 za MED, 3 za LOW, s limitem na kontrolu (max 50 / 18 / 6),
  aby deset nálezů jedné kontroly nesrazilo repo na nulu. Kalibrováno podle wireframu.
  Pod 30 body se skóre jen blíží nule, viz „Skóre bez nul“ (#37).
- **Skupiny triáže podle nejzávažnějšího nálezu (#73):** HIGH / MED / LOW / BEZ NÁLEZŮ
  (+ NELZE ZKONTROLOVAT). Dřívější pásma KRITICKÉ / SLEDOVAT / BEZ TEPU / ZDRAVÉ míchala dvě osy
  (závažnost a aktivitu repa) a barva skóre u nich neodpovídala skupině. Tep zůstává jen jako sloupec
  a v hlavičce; `limits.no_pulse_days` se už nepoužívá k seskupení, jen k souhrnu `no_pulse` v reportech.
  Barva skóre a proužku repa je barva jeho skupiny. `repos[].band` v JSON reportu má nové hodnoty
  (`high`/`medium`/`low`/`none`/`unchecked`), proto `schema_version` 2.

## Kontroly

- **Nález „nekompletní sken“ má vlastní ID `scan-incomplete`** (LOW), aby šel vysvětlit
  (`explain`) i allowlistovat. Vzniká při vypršení `limits.history_timeout_s`.
- **Chybějící lockfile** je samostatná kontrola `deps-lockfile-missing` (kategorie
  Bezpečnost, funguje offline). U Rustu jen pro aplikace (`src/main.rs`) – knihovny
  lockfile commitovat nemusí.
- **`deps-outdated`** hlásí jeden nález za úroveň (major = MED, minor/patch = LOW) místo
  nálezu za balíček – jinak by zastaralé závislosti přehlušily vše ostatní. Kontrolují se
  jen přímé závislosti (PyPI a npm, jak požaduje zadání).
- **OSV:** závažnost se dohledá přes `/v1/vulns/<id>` (max. 50 ID na sken, cache 24 h);
  bez údaje → MED.
- **`gitignore-incomplete`** ověřuje pravidla skutečným `git check-ignore --no-index`
  proti reprezentativním cestám a ignoruje globální excludes uživatele (nesdílí se).
- **`public-sensitive`** skenuje jen konfigurační soubory (yml, ini, toml, json, env, tf,
  inventáře…), ne dokumentaci – ukázková IP v README by jinak dělala falešné poplachy.
  Bez znalosti viditelnosti je kontrola přeskočena. U GitHubu bez připojení se viditelnost
  zjišťuje neautentizovaným `GET /repos/{owner}/{repo}` (404 = neznámo).
- **Forgejo vs. Gitea:** společná implementace nad `/api/v1`. CI se čte z commit statusu
  výchozí větve (Gitea/Forgejo Actions ho nastavují), ochrana větve z `branches/{b}.protected`.
  Rozdíl řešíme jen v `test_connection` (upozornění, když je `type = "gitea"` ale instance
  je Forgejo). Bezpečnostní alerty Gitea/Forgejo/GitLab (Free) přes API neposkytují.
- **Přiřazení repa k hostingu** porovnává jen hostname (SSH port 2222 se liší od webového
  3000); při více účtech na stejném hostu rozhoduje shoda vlastníka s `user`/`org`.

## TUI a zkratky

- **Kolize zkratek** z výchozí tabulky zadání:
  - `1`–`8` přepínají obrazovky; filtry severity jsou `Alt+1`/`Alt+2`/`Alt+3` (filtr
    „≥ severity“, druhé stisknutí vypne).
  - `G` = konec seznamu, seskupení je `Alt+G` (triáž → složka → hosting).
  - `g`/`G` a `j`/`k` patří „pohybu“ a nesmí se přemapovat na nic jiného (pohyb je aktivní
    ve všech seznamech); globální klávesy kolidují se vším.
  - `f` (léčba) je stejná akce na přehledu i v kartě – to kolizí není.
  - `e` znamená „upravit“ ve Složkách/Hostingách a „exportovat“ v Exportu (jiný kontext).
  - `Esc` je vyhrazený (zpět / zrušit) a nejde přemapovat.
  - `space` v Léčbě přepíná opravu, ve Složkách zapíná/vypíná složku.
  - Vzdálená repa: `c` klon, `v` přepnutí pohledu, `Ctrl+R` znovu načíst.
- **Validace `[keys]`** proběhne při startu (`repo-doctor` skončí s chybou a exit kódem 2)
  i v `repo-doctor config validate`. V TUI (kdyby se konfigurace změnila za běhu) se chyba
  ukáže notifikací a použijí se výchozí zkratky.
- **Spodní lišta** je vlastní widget `KeyBar` (ne Textual `Footer`) – pořadí a výběr kláves
  je podle wireframu a na úzkém terminálu se vypouštějí méně důležité položky, `?` a `q`
  zůstávají vždy.
- **Nadpis panelu vlevo i vpravo** („repo-doctor · triáž … ● online“) se skládá do jednoho
  `border_title` podle aktuální šířky – Textual jinak umí jen jeden nadpis.
- **Průvodce „Přeskočit“** zapíše prázdnou konfiguraci, aby se průvodce neobjevoval znovu.
- **`q` na přehledu ukončí aplikaci bez dotazu** (nic se neztratí), `Ctrl+C` okamžitě.
- **Hledání `/`** filtruje podle názvu repa, cesty, ID kontroly a textu nálezu.
- **Historie „Od včerejška“** porovnává s předchozím uloženým skenem (ne nutně
  s kalendářním včerejškem) – posledních N skenů (`limits.history_size`, výchozí 30).
- ~~Barvy světlého tématu~~ – zrušeno v #89, viz níže.

## UI: jediné téma, bez přepínače panelu (#89)

- **Jediné téma, černé pozadí.** Světlé téma, klávesa `t` a volba „Vzhled“ v Nastavení jsou pryč;
  obrysy a texty mají základní zelenou `#00aa00`, barvy severit zůstávají (červená / jantarová / modrá),
  jinak by se z nálezů nedalo poznat, co hoří. Pozadí je vždy `#000000`: Textual při fokusu přidává
  světlý nádech (`background-tint`), kvůli kterému oblast přehledu při návratu do okna zešedla, proto
  je v `app.tcss` vypnutý.
- **Klávesa `b` (skrýt levý panel) a `ui.show_sidebar` jsou zrušené.** Levý panel se i nadále sám schová
  pod 100 sloupců (`SIDEBAR_MIN_WIDTH`), jinak by se nevešla tabulka.
- **Staré konfigurace se nerozbijí:** klíče `ui.theme` a `ui.show_sidebar` se při načtení tiše zahodí
  (`UIConfig._drop_removed_keys`), přestože jinak platí `extra="forbid"`.
- **Animace skenu je jen v nevyplněné (pravé) části pruhu;** zelená vyplněná část je jednolitá.

## MCP server (0.2.0)

- **SDK `mcp` 2.x** (`MCPServer`, dřív FastMCP). Logika nástrojů je v `RepoDoctorTools` bez
  závislosti na MCP – MCP vrstva (`build_server`) jen registruje funkce s docstringy.
- **Jen čtení:** nástroje pracují nad posledním výsledkem skenu (cache repo-doctoru);
  `scan` spouští běžný sken s `offline = not mcp.network` a `fetch = False`. Jediný zápis je
  `last-scan.json` v cache repo-doctoru – stejně jako po skenu v TUI. Všechny nástroje mají
  anotaci `readOnlyHint`.
- **Maskování:** nálezy už nesou jen maskované ukázky; výstupy navíc procházejí redakčním
  registrem a texty se zkracují (500 znaků). Stránkování `offset`/`limit` (max 100).
- **Podpříkazy CLI** se zjišťují z registru typeru (`subcommands()`), ne z ručního seznamu –
  nový příkaz (`mcp`) by jinak skončil v TUI větvi a stdio server by vypsal nápovědu.

## MCP: rozsah a zápisy (#38)

- **Výchozí stav zůstává jen čtení.** Zápisy jsou čtyři a zapínají se po jednom v `[mcp] writes`
  (Nastavení → MCP server): `fix_branch`, `allowlist`, `sync`, `publish`. Vypnutý zápis se vůbec
  neregistruje (klient ho nevidí) a `RepoDoctorTools` ho navíc odmítne, i kdyby ho někdo zavolal přímo.
- **Náhled a potvrzení v MCP:** každý zápisový nástroj má `confirm=false` = jen náhled (diff, plán,
  commity k odeslání); provede se až s `confirm=true`. Odpovídá zásadě „výchozí je Zrušit“ z TUI;
  potvrzuje navíc klient při schválení nástroje.
- **Zápis jen do sledovaných složek,** vždy, i při `restrict_to_roots = false`. `restrict_to_roots`
  (výchozí zapnuto) omezuje čtení: `scan` s cestou mimo složky se odmítne a repa mimo složky se
  skryjí ve výpisech. Dřívější `scan(repo=<libovolná cesta s .git>)` byla mezera.
- **Nad stávajícím jádrem, žádná nová logika:** `fixes.plan/apply` (jen nová větev), `store.add_allow`,
  `sync.fast_forward` (bez fetch, takže dožene jen to, co už je staženo), `publish.plan_push/apply_push`
  + `plan_pr/apply_pr`. Merge přes MCP není (ani lokální, ani na hostingu). Selže-li PR po úspěšném
  pushi, nástroj to hlásí otevřeně (`applied=true`, zpráva).
- **Maskování je pevné,** nedá se vypnout. `allowlist_add` bere otisk z `repo_findings` (`fingerprint`).
  Náhled diffu z `fix_branch` navíc schová řádky, které vypadají jako tajemství (i kontextové): `redact`
  zná jen hodnoty, které scanner už našel.
- **`allowlist_add` odmítne nálezy kategorie security.** Allowlist umlčí i pre-push hlídače, takže by šel
  řetězem `allowlist_add` → `publish_fix` obejít kontrolu tajemství. O výjimce pro bezpečnostní nález
  rozhoduje člověk v aplikaci. Při zápisu se `config.toml` načte znovu z disku (TUI mezitím mohl soubor
  změnit nebo zápis vypnout) a zápis se jinak odmítne.
- **Potvrzení není vázané tokenem na náhled:** model by náhled i potvrzení zavolal sám, takže token nic
  nechrání. Skutečnou bránou je schválení nástroje v klientovi; `sync_ff` má navíc `destructiveHint`
  (mění pracovní strom).
- **Mimo #38:** širší rozsah po přidání správy Git repozitářů řeší #96.

## Pre-push hlídač (0.2.0)

- **Dva směry hooků:** repo-doctor při *svých* voláních gitu hooky nikdy nespouští
  (`core.hooksPath=/dev/null`, 0.1.4). Hook `pre-push`, který léčba nainstaluje, spouští
  uživatelův *vlastní* `git push` – a ten volá `repo-doctor guard`. Tyto dvě věci se
  nepotkají: guard sám spouští git zase s vypnutými hooky.
- **Lokální hook** je jediný zápis do `.git/hooks`: jen hook `pre-push`, jen v repu uživatele
  (`apply` odmítne repo cizího vlastníka), existující cizí hook nepřepíše (pozná svůj podle
  značky) a při `core.hooksPath` se nenabízí, protože by se nespustil. Když `repo-doctor`
  chybí v PATH, hook push nezablokuje (jen vypíše varování).
- **Pre-commit varianta** používá `repo: local` s `entry: repo-doctor guard` – oficiální
  gitleaks hook pro pre-commit skenuje staged změny, což při pushi nedává smysl. Soubor se
  upraví jen tehdy, když jde blok bezpečně připojit (`repos:` je poslední klíč nejvyšší
  úrovně); jinak se nabídne jen lokální varianta.
- **Rozsah:** `lsha --not --remotes` pro novou větev, jinak `rsha..lsha`; z pre-commitu
  `PRE_COMMIT_FROM_REF..PRE_COMMIT_TO_REF`. Vestavěný skener (rychlý, bez závislostí);
  allowlist platí stejně jako v kartě repa.

## Pre-push hlídač: selhání = zastavení

- Když `git log` pro rozsah pushe selže (timeout, chybějící objekt), push se zastaví – hlídač,
  který při chybě pouští, nic nehlídá. Remote sha, který lokálně není (force push, nefetchnuto),
  se nahradí rozsahem „vše, co není na žádném remotu“.
- Hook se zapisuje s `O_NOFOLLOW`, nikdy přes symlink a jen do `.git/hooks` vlastněného
  uživatelem.

## Synchronizace (0.2.0)

- **Jediná funkce, která mění pracovní strom** – a jen `git merge --ff-only @{u}` (git sám
  odmítne cokoli jiného než fast-forward). Repo musí být na sledované větvi, pozadu a ne
  rozdělené, bez změn ve sledovaných souborech a bez nesledovaných souborů, které by
  fast-forward přepsal. Stav se těsně před provedením zjistí znovu.
- **Hooky zůstávají vypnuté** (`post-merge`, `post-checkout`…) – stejná pravidla jako všude.
- **Repo s vlastními filtry/drivery v `.git/config` se nedohání:** hardening by filtry
  (např. git-crypt, smudge) při checkoutu vypnul a do pracovního stromu by se zapsal
  nefiltrovaný obsah. Takové repo je potřeba dohnat ručně (`git pull --ff-only`).
- **Fetch je samostatný krok** (`f` v TUI, `--fetch` v CLI, výchozí nikde) a řídí se pravidly sítě z 0.1.4
  a `allow_http_hosts`.
- **Pracovní strom musí být samotné repo:** cizí `core.worktree` by fast-forward přesměroval
  jinam (třeba do `$HOME`), proto se repo, jehož `--show-toplevel` není jeho cesta, nedohání.
  Totéž platí pro úklid.
- **Ignorované soubory se počítají jako kolize:** git je při fast-forwardu považuje za
  postradatelné a přepsal by třeba lokální `.env`.
- Zbytkové riziko: globální git-lfs uživatele při checkoutu stahuje objekty podle `.lfsconfig`
  repa (mimo `allow_http_hosts`). Je to uživatelova důvěryhodná konfigurace a obsah repa
  si uživatel synchronizuje sám; repo-doctor tu nic nespouští navíc.

## Plánovaný sken a upozornění (0.2.0)

- **Uživatelský systemd timer**, ne cron ani root: `systemctl --user` je na Arch/Fedora
  standard, `Persistent=true` dožene zmeškaný běh po uspání, `Nice`/`IOSchedulingClass=idle`
  šetří stroj. Bez systemd se jen vypíše řádek pro cron – nic se nezapisuje do crontabu.
- **Upozorňuje se jen na změny** proti minulému skenu (otisky nálezů), jinak by se upozornění
  rychle ignorovala. První běh jen uloží výchozí stav.
- **Text upozornění nese jen názvy rep a počty** – ntfy i e-mail jsou cizí služby, cesty
  k souborům a maskovaná tajemství tam nemají co dělat.
- ntfy `http://` jen pro `allow_http_hosts` (ověří se při načtení konfigurace i při odeslání),
  přesměrování se nenásledují; token přes stejné zdroje jako u hostingů. E-mail jen přes
  lokální `sendmail` – žádné SMTP heslo v konfiguraci.
- Plánovaný sken ve výchozím stavu nefetchuje (`[schedule] fetch`).

## Výkon

- Repozitáře běží paralelně (`--jobs`, výchozí počet CPU) jako asyncio úlohy; git příkazy
  a kontroly ve vláknech (`asyncio.to_thread`), síť přes async httpx se semaforem (8).
- **Benchmark:** syntetická sada 50 repozitářů musí offline projít do **60 s** (test
  `tests/test_scanner.py::test_benchmark_50_repos`; v CI na 4 jádrech typicky ~5–10 s).
- OSV se ptá dávkově (`querybatch`, až 1000 balíčků na dotaz) po repech, aby se tabulka
  v TUI plnila průběžně; odpovědi se cachují na disk.

## Distribuce a aktualizace (0.3.0)

- **Samostatná aplikace místo uv/pipx (#21):** uživatel nemá řešit Python ani izolovaná
  prostředí. Systémový Python nestačí – Debian 12 má 3.11, Ubuntu 22.04 3.10 – a `textual`
  ani `mcp` v repozitářích distribucí nejsou. PyInstaller (onedir) přibalí Python 3.12 i knihovny;
  ~50 MB na disku, ~27 MB balíček. Onedir místo onefile: rychlý start bez rozbalování do /tmp.
- **Sestavení na Ubuntu 22.04 (glibc 2.35)** s Pythonem od uv (python-build-standalone).
  Debian 11 už nemá podporu (archiv bez balíčků). Jen x86_64.
- **Balíčky přes nfpm** z jedné konfigurace (`packaging/nfpm.yaml`). Aplikace v
  `/usr/lib/repo-doctor`, odkaz `/usr/bin/repo-doctor`. Značka `install-kind` vedle binárky
  (`deb`/`rpm`/`pacman`/`tarball`) říká aplikaci, jak radit s aktualizací.
- **Aktualizace přes správce balíčků:** `.deb`/`.rpm` přidají zdroj společného repozitáře
  Icenux (`/etc/apt/sources.list.d/icenux.list`, `/etc/yum.repos.d/icenux.repo`) a jeho
  veřejný klíč. Repozitář (plochý APT, createrepo_c) je v samostatném repu
  `Luquas95/icenux` na GitHub Pages, podepsaný (`InRelease`, `repomd.xml.asc`, RPM podepsané
  nfpm); icenux si hodinově bere nejnovější vydání aplikací z `apps.txt` a ověřuje podpis
  `SHA256SUMS`. Bez vlastní serverové infrastruktury, drží se jen poslední verze každé aplikace.
  Metadata podepisuje samostatný klíč
  Icenux (`keys/icenux.asc` v icenux), balíčky a `SHA256SUMS` dál klíč vydání repo-doctoru;
  RPM `gpgkey` proto obsahuje oba. Soubory zdroje **nepatří balíčku**: zapisuje je `packaging/scripts/postinst.sh` (jen když chybí),
  protože by se jinak další aplikace Icenux s repo-doctorem tloukly o jednu cestu v dpkg.
  Po `apt remove` proto zdroj Icenux zůstane. Postinst zároveň maže starý samostatný
  repozitář repo-doctoru (`…/repo-doctor/apt`), jehož klíč s aktualizací zmizí. Ten web dostal
  naposledy verzi 0.4.0 s přepnutím a dál se neplní (job `pages` a `packaging/repo.sh` jsou smazané);
  zůstává na posledním nasazeném stavu.
- **Samoaktualizace jen pro tarball** (balíčky by tím obešly správce balíčků). Podpis
  `SHA256SUMS` se ověřuje přes `gpgv`/`gpg` jen proti klíči zabudovanému v aplikaci
  (otisk `update.FINGERPRINT`), tarball se rozbaluje s `filter="data"`, výměna složek je dvojí
  přejmenování; stará verze zůstane v `.repo-doctor-old` do další aktualizace (běžící
  proces z ní může ještě načítat moduly).
- **Ochrana proti downgradu:** odpověď GitHub API (tag vydání) není podepsaná, proto tarball
  nese soubor `VERSION`, který kryje podepsaný kontrolní součet. Samoaktualizace odmítne
  vydání, které není striktně novější než běžící verze, i balík, jehož `VERSION` se liší
  od očekávané verze – podvržené API tak nevrátí starší, byť platně podepsané vydání.
- **Kontrola verze** jen v TUI, nejvýš jednou za 24 h (cache `update-check.json`),
  ne v offline režimu, vypnutelná `[updates] check`. Na GitHub jde jen anonymní dotaz na
  poslední vydání.
- **Podpisový klíč vydání** (RSA 4096, UID bez osobních údajů) je v secretu
  `RELEASE_SIGNING_KEY`; veřejná část v `src/repo_doctor/data/signing-key.asc`. Výměna klíče
  = nová verze aplikace s novým klíčem podepsaná ještě starým.
- uv zůstává jen vývojovým nástrojem (CI, `CLAUDE.md`). Kontroly, které rozpoznávají
  `uv.lock` v cizích projektech, se netýkají instalace a zůstávají.

## Volitelné nástroje (#24)

- **Root jen přes viditelné `sudo` v terminálu:** TUI se pozastaví (`App.suspend`) a správce
  balíčků běží interaktivně. Uživatel vidí přesný příkaz i závislosti a heslo zadává sám.
  Funguje i přes SSH. `pkexec` nepoužíváme, protože bez grafické relace nefunguje a skrývá,
  co se děje. Bez možnosti pozastavení (headless) se příkaz jen ukáže. `--yes` v CLI přidá
  `-y`/`--noconfirm`, v TUI se správce balíčků ptá sám.
- **Distribuce podle `/etc/os-release`** (`ID`, `ID_LIKE`) → `apt-get`, `dnf`, `pacman`,
  `zypper`, a jen když binárka správce existuje. Jinak se jen poradí.
- **`gitleaks` mimo distribuci** (Debian 12, Ubuntu 22.04 ho nemají): oficiální binárka
  z GitHub Releases s verzí a SHA-256 **připnutými v kódu** (`tools.GITLEAKS`). Součet
  stažený ze stejného místa by chránil jen před poškozením, ne před podvrhem. Z archivu se
  vybalí jen soubor binárky (žádné `extractall`). Ukládá se do `~/.local/share/repo-doctor/tools`,
  ne do `~/.local/lib/repo-doctor`: tu celou vyměňuje samoaktualizace tarballu. Zda ho
  distribuce má, zjistí dotaz bez roota (`apt-cache policy`, `dnf repoquery`, `pacman -Si`,
  `zypper search`). Verzi gitleaks zvedáme s vydáním repo-doctoru.
- **Klíčenka se neinstaluje:** samotný balíček nestačí, Secret Service musí běžet v přihlášené
  relaci. Ukáže se jen stav a že tokeny jdou přes `env`/`cmd`.
- Upozornění na chybějící `gitleaks` jen jednou (značka `~/.cache/repo-doctor/tools-hint`).

## Ukončení aplikace (#35)

- **Proč proces čekal:** Textual běží přes `asyncio.run`, které při ukončení čeká na vlákna
  výchozího executoru (`asyncio.to_thread`) – a v nich běžely git příkazy skenu. Zrušení
  asyncio úloh vlákno nezastaví.
- **Řešení:** všechny podprocesy gitu a gitleaks jdou přes `gitwrap.run_process` /
  `stream_lines`, které je evidují. `RepoDoctorApp.exit()` zruší sken a zavolá
  `gitwrap.abort_all()`: SIGTERM celé skupině procesů (vlastní session, aby padli i potomci
  jako ssh, kteří drží roury), po 2 s SIGKILL, a každé další spuštění vyhodí `GitAborted`.
  SIGTERM jako první proto, že git po něm uklidí zámky (`index.lock`).
- **Sken po spuštění je ve výchozím stavu vypnutý** (`ui.scan_on_start = false`), protože
  se ukazuje poslední uložený výsledek. Průvodce prvním spuštěním skenuje vždy.
- **Konec jen přes `ctrl+q`** (výchozí klávesa Textualu), `q` je jen „zpět“ – omylem
  zmáčknuté `q` na přehledu už nezavře aplikaci uprostřed skenu.

## Jedna funkce, jedna zkratka (#32)

- **Kontextové klávesy vyhrály nad čísly:** léčba `f`, karta `⏎`, zpět `q`/`esc`. Globální
  `1`–`3` jen duplikovaly totéž (a potřebovaly vybrané repo). Čísla zůstala obrazovkám, které
  na výběru repa nezávisí, a jsou přečíslovaná od `1`.
- **Akce může platit ve víc kontextech** (`KeyAction.also`): `open_fix` je jedna akce pro
  přehled i kartu repa, takže se přemapuje na jednom místě a kolize se hlídají v obou.
  Test `test_every_function_has_one_action` hlídá, aby se duplicity nevrátily.
- **Obrazovky se otevírají podle jména** (`open_screen('folders')`), ne podle čísla, aby
  čísla nebyla natvrdo v aplikaci. Název `switch_screen` nejde, je to vestavěná akce Textualu.
- **Staré názvy akcí v `[keys]` nerozbijí start** (`LEGACY_IDS`, `REMOVED_IDS`): přejmenované
  se převedou, odebraný `screen_overview` se ignoruje.

## Skóre bez nul a popisek hostingu (#37)

- **Pod 30 bodů se skóre jen blíží nule:** dřív stačily HIGH nálezy dvou kontrol (2 × 50)
  a skóre bylo 0, takže kritická repa se od sebe nedala rozlišit. Nad 30 body platí dál
  100 − srážka, takže pásma (40 a 70), historie i reporty běžných rep zůstávají stejné.
  Pod 30 je skóre `900 / (srážka − 40)`: navazuje spojitě a se stejným sklonem, s každým
  dalším nálezem klesá a minimum je 1. Čistě násobné skóre („podíl ze zbytku“) jsme
  zamítli, protože by posunulo i běžná repa mezi pásmy (7 MED: 37 → 52).
- **Při shodném skóre** se řadí podle počtu HIGH, MED a LOW nálezů.
- **Popisek hostingu u IP adresy** (remote bez nastaveného hostingu) je celá adresa, ne
  první oktet (`100` u Tailscale adres `100.x.x.x`).
- **Popisek známých služeb (#58):** `github.com`, `gitlab.com` a `codeberg.org` se bez
  nastavení ukazují jako `GitHub`, `GitLab`, `Codeberg` (dřív `github`), aby se repa
  nerozpadla do dvou skupin s připojeným hostingem. Self-hosted instanci (Forgejo, Gitea) bez
  sítě nepoznáme, proto zůstává adresa; jméno jí dá až přidání v Hostingech (2).

## Správa nástrojů (#31)

- **Vlastní obrazovka místo sekce v Nastavení:** sekce byla až na konci dlouhého formuláře
  a uživatel ji nenašel. Nástroje mají klávesu `8`, v Nastavení zůstal jen odkaz.
- **Spravujeme jen to, co repo-doctor sám stáhl:** stažená binárka má vedle sebe značku
  `<název>.version`, podle ní se pozná zastaralá verze (i binárka bez značky, tedy stažená
  dřív, se nabídne k aktualizaci). Odebrání maže jen soubory ve složce repo-doctoru.
  Systémové balíčky aktualizuje a odinstaluje správce balíčků. Odinstalace přes `sudo` by
  mohla odebrat i závislosti jiných programů.

## Editor (#40)

- **Editor zůstává volitelný:** potřebuje ho jen klávesa `o` v kartě repa (otevřít soubor
  nálezu na řádku). Bez něj vše ostatní funguje.
- **Výběr ze seznamu známých editorů v PATH** (`tui/util.KNOWN_EDITORS`) místo volného
  textu, s volbou „automaticky“ ($VISUAL → $EDITOR → vi) a „vlastní příkaz“ pro cokoli jiného.
  Hodnota v `[ui] editor` je dál příkaz jako dřív, stávající konfigurace platí.
- **Grafické editory na pozadí** (`start_new_session`, bez pozastavení TUI): `subprocess.run`
  by čekal na zavření okna. Terminálové editory TUI pozastaví jako dřív.

## Detaily repa (#36)

- **Obsah po konzultaci s uživatelem:** jazyky, velikost, ekosystém a závislosti, licence,
  stáří a aktivita, autoři, větve a tagy a z hostingu popis, témata, hvězdy, forky, issues
  a data. Vše v rozšířené hlavičce karty (volba uživatele, ne přepínací panel).
- **Jazyky jako lišta na GitHubu:** podle velikosti blobů v HEAD (`ls-tree -l`), jen
  programovací a značkovací jazyky (bez JSON/YAML/Markdown) a bez `vendor`, `node_modules`,
  `dist` apod. Bez závislosti na linguistu.
- **Autoři jen jménem** (`shortlog -sn --no-merges`), e-maily se nikam neukládají.
- **Issues z GitHubu bez PR:** `open_issues_count` je počítá spolu s PR, odečítáme otevřené PR.
- **Zalamování jen na vyšším terminálu** (≥ 32 řádků), na menším se řádky uříznou, aby
  zbylo místo na nálezy.
- **JSON report:** `repos[].details` přidáno bez zvýšení `schema_version` (jen nové pole).

## Překlady (#33)

- **gettext `.po` bez kompilace:** msgid je český text přímo v kódu (`_("…")`), překlady
  v `src/repo_doctor/locale/en.po` (standardní formát, Poedit). Malý parser v `i18n.py` čte
  `.po` za běhu – žádný `.mo`, build krok ani nová závislost. `i18n_tools` texty vytáhne
  z AST a katalog synchronizuje; test hlídá úplnost a zachování `{placeholderů}`.
- **Jazyk se určí při importu balíčku** (`--lang` → `REPO_DOCTOR_LANG` → `[ui] language` →
  `LC_ALL`/`LC_MESSAGES`/`LANG`, bez locale angličtina), protože texty v konstantách modulů
  (zkratky, popisky) se překládají při importu. Proto změna v Nastavení platí po restartu.
- **Fáze:** 1 = TUI, CLI, zkratky (+ popisky kategorií, pásem a nástrojů); 2 = zprávy
  kontrol, opravy a jejich dokumentace; 3 = reporty a zbytek. Test hlídá, že v už přeložených
  modulech nezůstal český text mimo `_()`.
- **Dokumentace kontrol anglicky** leží v `docs/checks/en/<id>.md` se stejnou strukturou
  a anglickými nadpisy sekcí („What the check looks for“, „Why it matters“, „Steps“, „Note“).
  `checkdocs.load` bere soubor podle jazyka rozhraní, bez překladu českou verzi. Markdown
  se nepřekládá přes `.po` – dlouhé texty s kódem jsou čitelnější jako celé soubory.
- **Otisky nálezů nezávisí na jazyku:** otisk (allowlist, upozornění) se počítá z `key`,
  bez něj ze zprávy. Jediný nález bez klíče (`no-remote`) proto dostal klíč rovný dřívější
  české zprávě, aby už povolené nálezy zůstaly povolené.
- **Názvy kontrol** jsou v atributu třídy přes `N_()` (import modulu probíhá dřív, než se
  zpracuje `--lang`) a překládají se až při vytvoření nálezu a v Nastavení.
- **Obsah zapisovaných souborů** (komentáře v `.gitignore`, název hooku v pre-commitu, šablony
  README/CI/pre-commit/.dockerignore a hook `pre-push`) se řídí jazykem rozhraní. Šablony jsou
  v konstantách přes `N_()` a překládá je `templates.load` (u pre-commitu se základ přeloží před
  spojením s doplňky); licence (MIT, Apache…) zůstávají v anglickém originále.
- **Fáze 3 (reporty a zbytek jádra):** Markdown a HTML report i texty hostingů, tokenů, sítě,
  úklidu, synchronizace, aktualizací, plánování a oznámení jsou přeložené; test
  `TRANSLATED` hlídá, že v nich nezůstal český text mimo `_()`. Popisy v JSON schématu
  (`docs/report-schema.json`) jsou napevno anglicky – je to strojový kontrakt, který by se jinak
  měnil podle jazyka při generování. Sdílený popisek viditelnosti je `reports.visibility_label`.
- **Mimo překlad zůstává:** ladicí logy (`log.debug`, anglicky), `i18n_tools` (vývojářský nástroj)
  a chyba `licenses` při parsování (interní), MCP server (už je anglicky).
- **Množné číslo:** `czech.plural(n, one, few, many)` – anglicky jen `one`/`many`.

## Repo a proces

- **Branch:** zadání chtělo `feat/initial-implementation`, prostředí ale vyžaduje vývoj na
  přidělené branchi `claude/new-session-32njj2`. Použili jsme ji (obsah je stejný).
- **Dokumentace kontrol** leží v `docs/checks/<id>.md` a do wheelu se kopíruje jako
  `repo_doctor/_checkdocs` (hatch `force-include`), takže `explain` funguje i po instalaci.
- **Python 3.12+:** zadání; CI testuje 3.12 a 3.13.

## Odeslání oprav (push, PR, merge) – #39

- **Záruka se mění:** dřív „repo-doctor nikdy nepushuje“, teď „nepushuje bez výslovného pokynu“.
  Sken, léčba, úklid, synchronizace i MCP server dál nic neposílají; zápis na remote a hosting
  mají jen kroky v `publish.py`, každý samostatný, s náhledem a potvrzením (CLI: `--apply`).
  Funkce je zapnutá hned (žádný přepínač), protože každý krok vyžaduje vědomou akci.
- **Jen opravná větev:** push smí poslat jen `repo-doctor/fixes-*` a po lokálním merge výchozí
  větev. Žádný force (git sám odmítne non-fast-forward), žádné tagy, `--no-recurse-submodules`,
  hooky vypnuté (`core.hooksPath`) a `--receive-pack=git-receive-pack` přebíjí
  `remote.*.receivepack` z cizí konfigurace (stejný důvod jako `--upload-pack` u fetch).
- **Kontrola tajemství před pushem:** `plan_push` pustí `run_guard` na rozsah „commity, které
  nejsou na žádném remote“; nález push zastaví (stejně jako hook `pre-push`).
- **Push bez našich tokenů:** jde přes SSH klíč nebo credential helper uživatele. Token hostingu
  z konfigurace se používá jen pro API (PR/MR, merge) – do gitu se nikdy nepředává.
- **Merge na hostingu jen po zelené CI:** `plan_merge` vyžaduje CI `success` nebo `none`
  (žádná CI), `mergeable` a shodu SHA hlavičky PR s lokální větví. `pending`, `failure`
  i neznámý stav merge nenabídnou. `apply_merge` stav načte znovu a hostingu pošle SHA hlavičky
  (GitHub `sha`, Gitea `head_commit_id`, GitLab `sha`), takže merge selže, pokud se větev změnila.
  Metoda: `[publish] merge_method` (`auto` = merge → squash → rebase podle toho, co repo
  povoluje); GitLab má metodu v projektu, jde volit jen squash.
- **Lokální merge jen fast-forward:** když je výchozí větev checkoutnutá, platí stejná pravidla
  jako u Synchronizace (čisté repo, žádné přepsání nesledovaných/ignorovaných souborů, žádné
  filtry v `.git/config`) a jde přes `merge --ff-only`. Jinak se posune jen ref
  (`update-ref` se starým SHA), takže pracovní strom ani index nezůstanou nikde napůl.
  Větev checkoutnutá v jiném worktree se odmítne. Rozdělená historie se neřeší – uživatel
  ji srovná ručně.
- **Žádná cache u zápisů:** `post_json` i `PUT` jdou s `ttl=0` a stav PR/CI se čte vždy čerstvě;
  cachovaná odpověď by jinak mohla vrátit starý PR nebo opakovat POST.
- **Zápisové metody forge klientů** (`create_pr`, `find_pr`, `get_pr`, `pr_ci`, `merge_pr`) jsou
  abstraktní na `Forge`; sken je nevolá. Chybu API doplňuje zpráva z těla odpovědi (`message`),
  zkrácená a prohnaná `redact`, ať je vidět proč hosting PR/merge odmítl.
- **Remote pro push:** `[publish] remote`, jinak remote výchozí větve, jinak `origin`, jinak jediný
  remote; víc remotů bez volby se odmítne. Hosting se hledá podle URL remotu (`match_forge`).
- **TUI jako tenká vrstva:** `tui/screens/publish.py` (`PublishFlow`) jen volá `plan_*`/`apply_*`
  ve vlákně a ukazuje je v `ConfirmDialog`; kvůli asyncio smyčce se hosting pro náhled i zápis
  připojuje zvlášť (`with_hosting`).

## Nápověda v TUI: jedna obrazovka, dvě záložky (#30)

- `?` = nápověda k aktuální obrazovce, `H` = úplná nápověda. Častá potřeba je „co tady můžu
  dělat?“, proto `?` zůstává kontextová; `F1` se nepoužil, protože ho řada terminálů zachytí.
- Texty (`tui/helptext.py`) jsou oddělené od zkratek (`keymap.py` zůstává jediným zdrojem
  pravdy). Obrazovka se hlásí atributem `help_context` (klíč z `keymap.CONTEXTS`);
  dialogy bez něj dědí kontext obrazovky pod sebou.
- Modální `HelpScreen` si zkratky `help` a `help_all` (i přemapované) přidává sám, protože
  modální obrazovka aplikační zkratky nepouští.

## Další metody analýzy, díl 1: hygiena stromu (#34)

- **Issue #34 se dělí na čtyři samostatné PR** (hygiena stromu, kvalita a zdraví projektu,
  lehká statická analýza, závislosti do hloubky). Spouštění testů se v tomto kole nedělá:
  znamenalo by spouštět cizí kód, což je proti zásadě, že sken jen čte.
- **Kontroly čtou jen index a pracovní strom.** Cíl symlinku (data z cizího repa) se řeší jen
  textově a na disk se nesahá, existence se bere z indexu; absolutní, UNC a `..` cíle se hlásí jako
  „míří mimo repo“ bez ověřování (jinak by šlo přes cizí symlink sondovat cesty nebo na Windows
  spustit síťové připojení); `.gitmodules` se čte přes `git config --file`; LFS kontrola nestahuje nic
  (LFS pointer je pod 1 KB, takže větší blob s atributem `filter=lfs` pointer být nemůže).
- **Nálezy po souborech jsou omezené na 25** (`MAX_FILES`), aby jedno rozbité repo nezahltilo report.
- **`dist/` a `build/` se mezi artefakty nehlásí:** bývá tam záměrně commitnutý výstup,
  falešné poplachy by převážily užitek. `target/` se hlásí jen s `Cargo.toml` v kořeni.
- **Konce řádků se berou z indexu** (`git ls-files --eol`), tedy z toho, co se commituje, ne
  z pracovního stromu, který ovlivňuje `core.autocrlf`.

## Další metody analýzy, díl 2: kvalita a zdraví projektu (#34)

- **Prahy proti šumu:** kontroly kódu (testy, lint, verze jazyka) se zapnou až od 5 zdrojových
  souborů (`SOURCE_MIN`), `vendor`, `node_modules` a testy se nepočítají. SECURITY.md se vyžaduje
  jen u repa, kterému hosting potvrdil, že je veřejné; CONTRIBUTING až od 3 autorů; CHANGELOG jen
  když existují tagy.
- **CI se neinterpretuje:** `tests-not-in-ci` hledá v textu CI souborů známé příkazy k testům.
  CI složené z jiných souborů (`uses: ./`, `include:`, `extends:`) nebo nečitelné se nehlásí,
  falešný poplach by byl horší než přehlédnutí.
- **Odkazy v README bez disku:** existence cíle se bere ze seznamu souborů (sledované +
  nesledované neignorované), stejně jako u symlinků. Cíl odkazu je text z cizího README.
- **Bot na závislosti** se hlásí jen u repa s remote a manifestem; konkrétní hosting se nerozlišuje
  (Dependabot i Renovate se počítají).

## Další metody analýzy, díl 3: lehká statická analýza (#34)

- **Nic se nespouští ani neimportuje.** Python jde přes `ast.parse` (syntaktické chyby a Python 2
  se tiše přeskočí), ostatní jazyky přes řádkové regexy. Plnohodnotná analýza (Semgrep, Bandit)
  se záměrně nevolá: jsou to cizí programy s vlastními pravidly a konfigurací z repa.
- **Cizí vstup je ohraničený:** soubory max. `limits.max_file_kb`, řádky max. 1000 znaků (delší je
  minifikovaný kód), regexy bez vnořených kvantifikátorů, max. 3000 souborů na kontrolu a 50 nálezů
  (nad to jeden nález „Nekompletní sken“, ne tiché oříznutí).
- **Jeden nález na soubor a pravidlo**, s prvním řádkem a počtem výskytů v `data["count"]`; klíč
  `py:<pravidlo>:<soubor>` je nezávislý na jazyku i na číslech řádků, takže allowlist drží i po úpravách.
- **Testy a vendorovaný kód se přeskakují** (v testech jsou rizikové vzory záměrně; shell skripty
  se čtou i v `tests/`, protože tam bývají nasazovací skripty).
- **`dangerouslySetInnerHTML` je LOW:** v Next.js layoutech je to běžný idiom (JSON-LD, úvodní
  skript s konstantou), MED by bylo hlučné.
- **Compose nemá čísla řádků:** `yaml.safe_load` je nenese, nález se váže na službu (`compose:<pravidlo>:<soubor>:<služba>`).

## Další metody analýzy, díl 4: závislosti do hloubky (#34)

- **Tři kontroly jsou čistě souborové** (`deps-lock-drift`, `deps-unpinned`, `deps-install-scripts`)
  a nepotřebují `RepoContext.deps`, takže běží i offline. Jen `deps-unmaintained` je síťová.
- **Žádné další dotazy na registry:** `deps.registry_info()` nahradila `latest_version()` (ta zůstala
  jako tenký obal). Z téže odpovědi PyPI/npm se bere nejnovější verze, `deprecated`, klasifikátor
  „Inactive“ a datum nahrání souborů poslední verze. Do `DepsData.registry` se ukládá jen
  s `want_registry`, tedy když je zapnutá `deps-unmaintained`.
- **Lock drift jen tam, kde je porovnání jednoznačné:** workspace (npm/pnpm `workspaces`,
  `[tool.uv.workspace]`, Cargo `[workspace]`) se přeskakuje, protože zámek je v kořeni a členové
  mají lokální balíčky. Rozbitý nebo nečitelný manifest či lockfile znamená „přeskočit“, ne pád.
- **Opuštěný = PyPI a 3 roky bez vydání** (konstanta `ABANDONED_YEARS`, ne konfigurace: nová volba
  by zasáhla obrazovku Nastavení). U npm datum vydání registr v `/latest` nevrací a plný
  dokument balíčku je těžký, proto jen `deprecated`.
- **`deps-install-scripts` je informativní (LOW):** část balíčků (esbuild, sharp) skripty potřebuje.
  Mlčí, když `.npmrc` obsahuje `ignore-scripts=true`.


## Falešné nálezy tajemství: zjevné vymyšlenosti (#74)

- **Posuzuje se celá hodnota, ne podřetězec:** heslo `Company123456` nebo `Qwerty123456` obsahuje
  řadu `123456`, ale je to reálné heslo, které filtr nesmí schovat (revize bezpečnosti). Řady musí
  tvořit aspoň 80 % hodnoty (`SEQUENCE_SHARE`).
- **Co je „zjevně vymyšlené“:** celá hodnota je zkrácený příklad s výpustkou (`abc...xyz`, nejvýš
  12 znaků na každé straně), z ≥ 80 % se skládá z řad písmen či číslic o délce ≥ 6
  (`SEQUENCE_RUN`) nebo je vzor s periodou do 4 znaků (`MAX_PERIOD`).
- **Filtr platí pro všechna pravidla kromě `private-key`.** `_looks_placeholder` zůstal beze změny,
  takže kontrola Dockerfile ENV se nezměnila.
- **Mimo rozsah:** klíče ve tvaru `wJalrXUt…` s opakovaným koncem a podobné hybridy se dál hlásí;
  ostatní případy řeší `repo-doctor:allow` a allowlist.


## Stránka Odeslání oprav a stav CI (#69)

- **Vlastní obrazovka `PublishScreen`** místo pěti kláves na kartě repa. Karta má jen `p`; klávesy
  `P`/`C`/`M`/`L`/`U` jsou nový kontext `publish` (přemapování v `[keys]` dál funguje, ID akcí zůstala).
- **Stav je čtecí:** `publish.publish_status()` složí místní část (opravná větev, je na remote?, jde
  lokální merge?, kolik commitů výchozí větve není na remote) a z hostingu PR, CI a překážky mergi.
  Překážky počítá společná `_merge_blockers`, kterou používá i `plan_merge`, takže stránka a merge
  nemohou říkat něco jiného.
- **Merge dál rozhoduje jen `apply_merge`**, který si stav i CI načte znovu těsně před zápisem;
  stránka merge nepovoluje, jen vysvětluje.
- **CI po jednotlivých kontrolách:** `CIStatus.checks` (`CICheck`: název, stav, odkaz) plní jen
  `Forge.pr_ci`; sken výchozí větve (`latest_ci_status`) zůstal u jediného souhrnného stavu, takže
  se v něm nic nezměnilo. GitLab kvůli tomu dělá jeden dotaz navíc (joby pipeline).
- **Automatické obnovování** jen dokud je CI `pending` (20 s, `REFRESH_SECONDS`); v offline režimu
  se hosting nevolá vůbec a stránka to řekne. Akce (push, PR, merge) offline fungují, jako dřív.
- **Mimo rozsah:** `repo-doctor publish status` v CLI a stav v MCP serveru (zatím jen TUI).




## Průběh CI jako graf jobů (#83)

- **Datový model:** `CIStatus.jobs` (`CIJob`: stav, čas, kroky, `needs`, stage) vedle `checks`; plní
  jen `Forge.pr_ci`. Tabulka kontrol zůstává (funguje všude), graf je volitelný pohled (`v`).
- **GitHub nevrací závislosti jobů**, proto se `needs` čtou z workflow souboru na commitu PR
  (`git cat-file`, jen `yaml.safe_load`, limit 256 kB, nic se nespouští). Job z API (`test (3.12)`,
  `volající / vnitřní`) se přiřadí k jobu z YAML podle nejdelší shody názvu nebo id; bez shody je bez
  závislostí. Joby i kroky dává `actions/runs?head_sha=` → `runs/{id}/jobs`; chyba nebo chybějící právo
  na Actions graf vynechá, souhrn CI se nerozbije.
- **GitLab:** sloupce jsou stages (API `needs` nedává); **Gitea/Forgejo:** jediný sloupec, joby
  seskupené podle „workflow / job“ z názvu statusu.
- **Rozložení (`pipeline.py`, čistá logika):** sloupec = nejdelší cesta; zdroje se posunou k následníkům;
  hrany vyplývající z jiné cesty se nekreslí (tranzitivní redukce). Hrana přes víc sloupců se nekreslí
  a u jobu je řádek „↳ po …“ – spojnice by musely vést přes jiné uzly.
- **Widget `PipelineView`** kreslí do mřížky znaků (box-drawing, spojnice skládané z bitových masek směrů)
  a animuje spinner jen když něco běží. Vzhled je aproximace GitHub Actions v terminálu, ne kopie.

## Zásada: žádné další programy; oprava zranitelných závislostí (#71)

- **Zásada:** repo-doctor se instaluje z balíčku a musí fungovat hned, stačí `git`. Nesmí vyžadovat
  ani spouštět `uv`, `npm`, `pip`, `cargo`… První verze opravy (#71) volala `uv lock` / `npm update`
  v dočasné složce – zamítnuto, protože uživatel by musel nástroj doinstalovat (přesně to issue kritizuje)
  a spouštění programů nad cizím repem je navíc bezpečnostní riziko (konfigurace, env, git/URL závislosti).
  Zásada je v `CLAUDE.md` a hlídá ji `tests/test_no_external_tools.py` (kdo smí spouštět procesy,
  které programy jsou volitelné, zákaz správců balíčků).
- **Čistě Pythonový přepis lockfilu (`lockfiles/`):** změní se jen verze jednoho balíčku; verze, odkaz
  a hash se berou z PyPI JSON API / npm registru (`HttpRegistry`, sdílí cache a síťová pravidla).
  Ruční přepis je jinak nebezpečný (hashe, strom závislostí), proto **odmítá**, co neumí bezpečně: nová
  verze s jinými závislostmi, omezení jiného balíčku (npm: mini-semver `lockfiles/semver.py`; uv:
  `requires_dist` závislých balíčků z PyPI), jiný než veřejný registr, neobvyklé formátování, větve řešení
  a `exclude-newer` v `uv.lock`, alias/workspace v npm, změna instalačních skriptů, zastaralé vydání.
- **Ověření formátu `uv.lock`:** před zápisem se z metadat **staré** verze vykreslí její `sdist` a `wheels`
  (pořadí podle názvu souboru, výběr podle `requires-python`, čas zkrácený na milisekundy) a musí to
  přesně sedět na to, co v lockfilu je. Tak se nikdy nezapíše tvar, který neumíme zreprodukovat.
  Fixtury v `tests/fixtures/lockfiles` jsou skutečné výstupy `uv lock` a `npm install --package-lock-only`
  (verze připnuté `--upgrade-package X==Y`); test vyžaduje shodu textu.
- **Cílová verze:** `deps.fixed_in` bere z OSV (`affected[].ranges[].events[].fixed`) u každé zranitelnosti
  nejnižší opravenou verzi vyšší než současná a z nich nejvyšší. Nález je `fixable`, když opravená verze
  existuje a lockfile je podporovaný (závisí jen na datech, ne na programech v `PATH`); v nálezu je
  `package`, `version`, `fixed_in`, aby léčba nepotřebovala síťová data skenu.
- **Mimo rozsah:** Cargo, Go, yarn, pnpm, poetry, npm lockfileVersion 1–2 – zatím ruční postup. Odmítnutou
  opravu léčba nenabídne (důvody odmítnutí jdou jen do poznámek úspěšné opravy).

## Vyléčit a dotáhnout do mergi, notifikace (#70)

- **Jedna větev, jeden PR:** vybrané opravy skončí v jedné větvi (commit na kontrolu, jak dělá léčba) a jde
  o ně jeden PR. Varianta „PR na každou opravu postupně“ se zamítla – vyžadovala by po každém mergi stahovat
  výchozí větev a čekat na CI znovu; jedna špatná oprava ale zablokuje celý PR (cena za jednoduchost).
- **Řetězí se jen existující kroky** (`fixes.apply`, `publish.plan_/apply_push`, `plan_/apply_pr`,
  `plan_merge`/`apply_merge`) v `workflow.py`; žádná nová cesta zápisu. Merge dál rozhoduje jen `apply_merge`
  a před zápisem znovu načte stav včetně CI. Ruční kroky na stránce Odeslání se nezměnily.
- **CI hned po vytvoření PR bývá `none`**, což `_ci_blocker` bere jako „jde mergnout“. Workflow proto
  čeká až `CI_GRACE_SECONDS` (90 s), než se CI objeví; když se nespustí, **zastaví se a nemerguje naslepo**
  (hláška „mergni ručně“). Repo bez CI tak potřebuje ruční merge. Dotazování po 15 s, strop 45 minut
  (konstanty v `workflow.py`, testy je přepisují). Čekání se počítá součtem intervalů, ne hodinami.
- **Hosting se ověří před léčbou** (`publish.connect`), aby kvůli chybějícímu tokenu nevznikala větev zbytečně.
- **Zastavení = výsledek, ne výjimka:** `Stop` nese `Event` (druh, úroveň, text, odkaz). Opravná větev a PR
  zůstávají; `publish_existing` (klávesa `A` na stránce Odeslání) je idempotentní – přeskočí hotový push
  a existující PR, takže nepotřebuje žádný soubor se stavem workflow.
- **Běh na pozadí aplikace, ne na obrazovce:** `WorkflowRunner` (`tui/workflow_runner.py`) spouští vlákno přes
  `App.run_worker`, takže přežije přechod mezi stránkami; zavřením aplikace (`stop_work`) se workflow přeruší.
  Nejvýš jedno workflow na repo. Odpojený běh (CLI, schedule) je mimo rozsah.
- **Notifikace** (`inbox.py`): jen události léčby, `inbox.json` v datové složce (práva 0600, atomický zápis,
  posledních 100, zprávy přes `redact`, odkaz jen `http(s)://`). Průběh „CI běží“ se neukládá, je jen živý
  řádek (`⟳`) v bloku. Blok je v levém panelu pod složkami; `⏎`/klik otevře `PublishScreen` repa, zmizelé
  repo ukáže jen upozornění. Desktopové upozornění (`notify.send_desktop`) dostane jen zastavení s chybou či varováním.
- **Mimo rozsah:** zprávy z plánovaného skenu v notifikacích, více repo najednou, CLI příkaz.

## Bez uv ve vývoji, CI i vydání (#86)

- **Aplikace uv nepotřebuje** (a nespouští ho): hlídá `tests/test_no_external_tools.py` (#71). Od #86 ho
  nepotřebuje ani vývoj, hlídá `tests/test_no_uv.py`. Dřívější rozhodnutí „uv zůstává vývojovým nástrojem“
  (#21, #71) se tímto ruší.
- **Zámky jsou `requirements-dev.txt` a `requirements-build.txt`** s hashi, generované `pip-compile --generate-hashes
  --allow-unsafe` z `pyproject.toml` (příkazy v `CLAUDE.md`). Instalují se `pip install --require-hashes`, takže
  podvržený balíček s jiným hashem selže. Skupiny `dev` a `build` jsou nově `[project.optional-dependencies]`,
  protože `pip-compile` nezná PEP 735 skupiny. Soubory leží v kořeni, aby je repo-doctor při skenu sám sebe
  poznal jako lockfile (`requirements*.txt` vedle `pyproject.toml`).
- **Omezení:** `pip-compile` zamyká pro Python, kterým běžel (3.12). Na 3.13 se instaluje stejná sada, bez
  vlastního rozlišení. Dokud se závislosti mezi verzemi Pythonu neliší, nevadí to; matice CI to ověřuje.
- **CI a vydání:** `actions/setup-python` + `pip`. Sestavení vydání dál běží v kontejneru `ubuntu:22.04`
  (glibc 2.35); Python už nedodává python-build-standalone od uv, ale sada akcí `setup-python`.
- **Node.js 20 v akcích:** `actions/checkout` v6.1.0 a `actions/setup-python` v6 (oboje `using: node24`),
  připnuté na SHA. Šablony CI pro cizí repozitáře používají `@v6`.
- **Šablona CI pro Python** už nepředpokládá `uv`: `pip install` (`requirements.txt`, `pip install -e .`
  jen když existuje `pyproject.toml`/`setup.py`), pak `ruff` a `pytest`.
- **Zůstává (funkce produktu):** rozpoznávání `uv.lock` v cizích projektech, jeho přepis při opravě
  zranitelných závislostí (`lockfiles/pypi.py`) a zmínky `uv` v dokumentaci kontrol jako příklady.

## Zjevně vymyšlené hodnoty i u gitleaksu

- **Příčina:** s nainstalovaným `gitleaks` kontroly `secrets-tree` a `secrets-history` bral výsledky jen od něj
  a filtr vymyšlených hodnot (`is_obvious_fake`) byl jen ve vestavěném skeneru. Testovací klíče typu
  `sk_live_AbCdEf…` se proto hlásily jen na strojích s gitleaksem (výsledek závisel na tom, co je nainstalováno).
- **Rozhodnutí:** `run_gitleaks` označí každou shodu (`SecretMatch.fake`) podle hodnoty `Secret` a kontrola ji
  vynechá. Posuzuje se jen tvar celé hodnoty (řada, střídavé řady, perioda, zkrácený příklad). Placeholder slova
  (`example`, `fake`…) se u gitleaksu **nepoužívají**, protože jeho pravidla mají pevné prefixy a slovo ve skutečném
  hesle by nález skrylo. Privátní klíče se nefiltrují nikdy.
- **Průhlednost:** počet skrytých hodnot (podle otisku, strom i historie dohromady) je v `RepoResult.hidden_fakes`,
  v Markdown, HTML i JSON reportu. Filtr jde vypnout `hide_fake_secrets = false`; volba je jen globální, takže
  `.repo-doctor.toml` cizího repa ji nezmění.
- **Vědomě neděláme:** nevylučujeme složku `tests/`; skutečné klíče se do testů dostávají omylem a vyloučení by je
  skrylo celé. Skutečné, ale triviální heslo ve tvaru řady (`abcdef123456`) se skryje (stejně jako dosud ve
  vestavěném skeneru).
