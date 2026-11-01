# Changelog

Formát podle [Keep a Changelog](https://keepachangelog.com/cs/1.1.0/), verze podle SemVer.

## [Nevydáno]

## [0.4.0] – 2026-11-01

### Změněno
- **Repozitář balíčků je teď společný Icenux** (`luquas95.github.io/icenux`), ne samostatný
  repozitář repo-doctoru. Balíček `.deb`/`.rpm` při aktualizaci sám přepne zdroj a starý
  smaže; nic dělat nemusíš. Po `apt remove` zdroj Icenux zůstane (patří všem aplikacím).

### Přidáno
- **Hygiena stromu a historie (#34, 1/4):** sedm nových kontrol, které nepotřebují síť
  a nespouštějí žádný kód z repa: `conflict-markers` (nevyřešené značky konfliktu),
  `committed-artifacts` (`node_modules`, `__pycache__`, `.venv`, `*.pyc`, `.DS_Store`…
  v gitu), `broken-symlinks` (existence cíle se bere z indexu, symlinky mířící mimo repo se jen hlásí), `case-collisions` (cesty lišící se jen velikostí písmen),
  `line-endings` (smíšené LF/CRLF a chybějící pravidlo v `.gitattributes`),
  `submodule-state` (submodul bez záznamu v `.gitmodules`, zbylý záznam, nestažený submodul,
  `http://` / `git://` adresa) a `lfs-not-applied` (soubor určený pro Git LFS uložený celý v gitu).
  Do `gitwrap.Git` přibyly `ls_files_stage()` a `ls_files_eol()`.
- **Kvalita a zdraví projektu (#34, 2/4):** sedm dalších kontrol čistě ze souborů, nic se
  nespouští: `tests-missing` (kód bez testů), `tests-not-in-ci` (testy existují, ale CI je
  nespouští), `lint-config-missing` (Python a Node bez linteru či formátovače),
  `runtime-unpinned` (nepřipnutá verze Pythonu či Node), `project-docs-missing` (CHANGELOG u repa
  s tagy, SECURITY.md u veřejného repa, CONTRIBUTING od tří autorů), `dependency-bot-missing`
  (Dependabot či Renovate) a `readme-links-broken` (relativní odkazy v README na neexistující
  soubory). Kontroly kódu mlčí u repí s méně než 5 zdrojovými soubory.
- **Lehká statická analýza (#34, 3/4):** čtyři bezpečnostní kontroly, které kód jen čtou
  (Python se parsuje přes `ast`, nic se nespouští ani neimportuje): `python-risks`
  (`eval`/`exec`, `shell=True`, `os.system`, `pickle`, `yaml.load`, `verify=False`,
  `debug=True`, holé `except:`, ladicí zbytky), `js-risks` (`eval`, skládané `exec`, `innerHTML`,
  vypnuté TLS, `dangerouslySetInnerHTML`, `debugger`), `shell-risks` (stažený skript puštěný
  v shellu, `chmod 777`, `rm -rf "$VAR"/…`, skript bez `set -e`) a `compose-risks`
  (`privileged`, `docker.sock`, `network_mode: host`, `cap_add`, databáze na všech rozhraních).
  Testy a vendorovaný kód se přeskakují, nálezů je nejvýš 50 na kontrolu.
- **Závislosti do hloubky (#34, 4/4):** čtyři kontroly nad manifesty a lockfily: `deps-lock-drift`
  (deklarovaná závislost chybí v `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `uv.lock`,
  `poetry.lock` či `Cargo.lock`), `deps-unpinned` (`*`, `latest`, bare řádek v `requirements.txt`,
  git závislost bez `rev`/`@ref`), `deps-install-scripts` (balíčky s `hasInstallScript`
  v npm lockfilu, mlčí s `ignore-scripts=true`) a síťová `deps-unmaintained` (npm `deprecated`,
  PyPI „Inactive“ a poslední vydání starší než 3 roky). Poslední jmenovaná využívá odpovědi
  registrů, které sken stahuje i pro `deps-outdated`, takže nepřibývají žádné dotazy
  (`DepsData.registry`, `deps.registry_info()`).
- **Nápověda v TUI (#30):** dva druhy v jedné obrazovce se dvěma záložkami (přepíná `Tab`).
  `?` otevře nápovědu k aktuální obrazovce (k čemu slouží, jak se s ní pracuje, její zkratky,
  pohyb a globální zkratky), nová `H` (akce `help_all`, jde přemapovat) rovnou úplnou nápovědu:
  postup práce, přehled obrazovek, pojmy, bezpečnostní zásady a všechny zkratky. Texty jsou
  v `tui/helptext.py`, zkratky dál z `keymap.py`; každá obrazovka má `help_context` a test hlídá,
  že nový kontext nezůstane bez textu. Dialogy bez vlastní nápovědy ukážou nápovědu obrazovky pod nimi.

### Opraveno
- **Popisek hostingu u nepřipojeného remote (#58):** u repa na vlastním Forgejo/Gitea
  (např. `100.x.x.x:3000`), které není v Hostingech (2), se ukazuje celá IP adresa. Je to záměr
  (#37): bez nastavení se název hostingu nedá poznat. Známé služby se teď ukazují jménem
  (`GitHub`, `GitLab`, `Codeberg`), shodně s připojeným hostingem. Self-hosted hosting
  se pojmenuje přidáním v Hostingech (2).
- **Sken v hlavičce (#57):** „x zruší sken“ zůstávalo vidět i po dokončení skenu (poslední
  překreslení proběhlo, dokud worker ještě běžel). Ukazatel průběhu je nově zelený a za běhu
  skenu v něm zprava doleva létají černé tečky (náhodně, i víc ve sloupci). Z hlavičky zmizel
  i dovětek „R nový sken všech rep“, zkratka je v dolní liště.
- **Nástroje (klávesa 8): výběr, co nainstalovat (#56):** `i` nově nainstaluje jen nástroj pod
  kurzorem, `I` doinstaluje všechny chybějící (dosavadní chování `i`).

## [0.3.1] – 2026-10-27

### Přidáno
- **Odeslání oprav: push, PR/MR a merge (#39):** po léčbě jde opravnou větev
  `repo-doctor/fixes-<datum>` poslat, otevřít z ní PR/MR a mergnout, každý krok zvlášť
  a s náhledem a potvrzením (výchozí „Zrušit“). V kartě repa `P` push, `C` PR/MR, `M` merge
  na hostingu, `L` lokální merge a `U` push výchozí větve; v CLI
  `repo-doctor publish push|pr|merge|merge-local [--branch B] [--apply]` (bez `--apply` jen náhled).
  - Push: jen opravná větev (a po lokálním merge výchozí), nikdy force ani tagy, hooky se
    nespouštějí, nové commity projdou kontrolou tajemství jako `guard`.
  - PR/MR: GitHub, GitLab, Gitea i Forgejo, popis z commitů; token potřebuje právo zápisu.
  - Merge na hostingu: jen když je CI zelená (nebo žádná není) a hosting hlásí `mergeable`;
    hosting dostane SHA hlavičky, takže merge selže, pokud se větev mezitím změnila. Metoda podle
    `[publish] merge_method` (`auto` = první povolená v repu).
  - Lokální merge: jen fast-forward, pracovní strom se nemění, pokud výchozí větev není
    checkoutnutá; funguje i bez remote.
  - Záruka „nikdy nepushuje“ se mění na „nepushuje bez výslovného pokynu“; sken, MCP server
    i léčba dál nic neposílají. Chyby API hostingu nově nesou jeho vlastní zprávu.
- **Angličtina (#33, fáze 1):** TUI, CLI a zkratky jsou i anglicky. Jazyk se volí podle
  systému (`LANG`/`LC_ALL`: cs_* → čeština, jinak angličtina), v Nastavení / `[ui] language`
  (`auto` | `cs` | `en`), přepínačem `--lang` nebo proměnnou `REPO_DOCTOR_LANG`. Změna
  v Nastavení platí po restartu.
- **Angličtina (#33, fáze 2):** anglicky jsou i nálezy kontrol (názvy, zprávy, důvody
  přeskočení), návrhy oprav a jejich chybová hlášení a dokumentace kontrol
  (`docs/checks/en/`, `explain` a „proč to vadí“ v kartě).
- **Angličtina (#33, fáze 3):** anglicky jsou i reporty (Markdown, HTML s `lang`), šablony
  README/CI/pre-commit/.dockerignore, hook `pre-push`, hlášky hostingů a tokenů, sítě, úklidu,
  synchronizace, aktualizací, plánování a oznámení. Popisy v JSON schématu jsou napevno
  anglicky (schéma je strojový kontrakt, nezávislý na jazyku rozhraní).
- **Detaily repa v kartě (#36):** hlavička karty ukazuje navíc
  - KÓD: jazyky (podíl podle velikosti), počet souborů, velikost a `.git`, licenci,
  - ZÁVISLOSTI: ekosystém, správce balíčků, přímé / všechny závislosti,
  - HISTORIE: první commit, počet commitů, aktivitu za 30 dní a rok, autory, větve, poslední tag,
  - NA HOSTINGU: popis, témata, hvězdy, forky, issues, kdy repo vzniklo a poslední push.

  Vše kromě hostingu se počítá offline. Detaily jsou i v JSON reportu (`repos[].details`).
- **Doinstalace volitelných nástrojů (#24):** `repo-doctor tools [--install] [--yes]`
  a sekce **Nastavení › Volitelné nástroje** ukážou stav `gitleaks`, `wl-clipboard`,
  `xdg-utils`, `libnotify` i klíčenky. Po potvrzení doinstalují chybějící: systémové balíčky
  přes `apt-get`/`dnf`/`pacman`/`zypper` (distribuce podle `/etc/os-release`, viditelně
  v terminálu přes `sudo`), `gitleaks` tam, kde ho distribuce nemá, stažením do
  `~/.local/share/repo-doctor/tools` s ověřením připnutého SHA-256 (8.30.1).
- Sken tajemství používá i `gitleaks` stažený repo-doctorem.
- **Obrazovka Nástroje (`8`, #31):** volitelné nástroje se spravují na vlastní obrazovce
  místo sekce na konci Nastavení. Ukáže původ (systém / stažený repo-doctorem i s verzí),
  `i` doinstaluje chybějící, `u` aktualizuje stažený `gitleaks` na připnutou verzi a `d` ho
  odebere. V CLI totéž: `repo-doctor tools --update` a `--remove NÁSTROJ`.
- TUI jednou upozorní, že sken tajemství by byl s `gitleaks` přesnější.

### Změněno
- **Srozumitelnější popisky (#41):**
  - Hlavička přehledu ukazuje „R nový sken všech rep“, v liště je `R sken všech rep`
    i `r sken repa`.
  - Popisky akcí v nápovědě, paletě a liště říkají, co se stane („skenovat všechna repa“,
    „skrýt nález (allowlist)“, „přesunout do koše“…).
- **Výběr editoru (#40):** Nastavení nabídne editory nainstalované v systému, volbu
  „automaticky ($VISUAL / $EDITOR)“ a vlastní příkaz. Vysvětlí, k čemu editor je (klávesa
  `o` v kartě repa), ukáže, který se opravdu spustí, a upozorní, když není nainstalovaný.
- **Jedna funkce = jedna zkratka (#32):** léčbu otevírá jen `f` (na přehledu i v kartě
  repa), kartu repa `⏎`, na přehled se vracíš `q`/`esc`. Globální `1`–`3` zmizely a ostatní
  obrazovky se přečíslovaly: složky `1`, hostingy `2`, vzdálená repa `3`, nastavení `4`,
  export `5`, úklid `6`, synchronizace `7`. Akce `dash_fix` a `detail_fix` se sloučily do
  `open_fix`. Staré názvy v `[keys]` dál fungují (`screen_fix` → `open_fix`,
  `screen_detail` → `open_card`, `screen_overview` se ignoruje).
- **Sken po spuštění je volitelný (#35):** TUI po startu ukáže poslední uložený výsledek;
  rovnou skenovat začne jen se zapnutou volbou **Nastavení › Sken po spuštění aplikace**
  (`[ui] scan_on_start = true`). Po prvním nastavení v průvodci se skenuje vždy.
- **Konec je `ctrl+q` (#35):** `q` už aplikaci nezavírá, jen vrací o obrazovku zpět.

### Opraveno
- Hlášky odkazovaly na stará čísla obrazovek (Složky „4“, Hostingy „5“) z doby před #32.
- Grafický editor (gedit, Kate, VS Code…) se spouští na pozadí. Dřív TUI zamrzlo, dokud
  se okno editoru nezavřelo. gedit a Kate otevírají soubor nálezu na správném řádku.
- **Skóre už nespadne na 0 (#37):** pod 30 body se jen blíží nule, takže repa s mnoha
  vážnými nálezy se dál liší a nejhorší je nahoře. Skóre od 30 výš se nemění. Při shodném
  skóre se řadí podle počtu HIGH, MED a LOW nálezů.
- Sloupec HOSTING u remote na IP adrese ukazoval jen první číslo (`100` místo
  `100.121.109.115`).
- Příkazová paleta u příkazu „Konec“ ukazovala `Ctrl+C` místo skutečné klávesy.
- **Zavření aplikace zastaví veškerou práci (#35):** běžící sken se zruší a git i gitleaks
  podprocesy (včetně jejich potomků) se ukončí. Proces dřív po zavření TUI čekal, než
  rozběhnuté git příkazy doběhnou.

## [0.3.0] – 2026-10-16

### Změněno
- **Instalace bez Pythonu a dalších prostředí (#21):** repo-doctor se vydává jako samostatná
  aplikace (PyInstaller, vlastní Python i knihovny) v balíčcích `.deb` (Debian/Ubuntu),
  `.rpm` (Fedora/RHEL), AUR `repo-doctor-bin` a jako tarball s `install.sh` pro ostatní
  distribuce; jen x86_64. Instalace přes uv/pipx už není podporovaný způsob a z dokumentace zmizela.
- PKGBUILD v `packaging/aur/` je teď šablona balíčku `repo-doctor-bin` (sestavená aplikace),
  hotový PKGBUILD je přiložený ke každému vydání.

### Přidáno
- Aktualizace: balíčky `.deb`/`.rpm` přidají podepsaný APT/DNF repozitář na GitHub Pages,
  takže nové verze přicházejí s aktualizací systému.
- `repo-doctor update [--check]`: zjistí novou verzi; instalaci z tarballu aktualizuje sám
  po ověření podpisu `SHA256SUMS` klíčem zabudovaným v aplikaci a kontrolního součtu.
- TUI jednou denně upozorní na novou verzi s návodem podle způsobu instalace
  (`[updates] check`, v offline režimu se nekontroluje).
- Workflow `release.yml`: tag `vX.Y.Z` sestaví aplikaci, otestuje instalaci na Debianu 12,
  Ubuntu 22.04/24.04, Fedoře a Archu, vytvoří GitHub Release a nasadí repozitáře na Pages.

### Opraveno
- Plánovaný sken v sestavené aplikaci spouští přímo její binárku (ne `python -m`).

## [0.2.0] – 2026-10-14

### Přidáno
- `repo-doctor mcp`: MCP server (stdio) jen pro čtení – nástroje `list_repos`, `repo_status`,
  `repo_findings`, `search_findings`, `last_scan_summary`, `scan`, `explain_check`; offline
  ve výchozím stavu (`[mcp] network`), maskované výstupy, stránkování.
- Kontrola `email-exposure`: tvůj osobní e-mail v historii veřejného repa (maskovaný
  `l***@proton.me`); léčba nastaví v repu lokálně `user.email` na noreply adresu hostingu.
- Kontrola `ci-security`: nepřipnuté akce, `pull_request_target` s kódem z PR, injekce výrazů
  do `run:`, široká oprávnění tokenu, tajemství v logu, self-hosted runner ve veřejném repu;
  léčba připne akce na SHA zjištěné přes API GitHubu (`@<sha> # v4`).
- Kontrola `duplicate-clone`: stejný remote (SSH/HTTPS, aliasy) nebo kořenový commit napříč
  sledovanými složkami, s porovnáním stavu klonů; přehled jde seskupit podle remotu (`alt+g`).
- Kontrola `license-compat`: licence přímých závislostí (PyPI, npm, crates.io) vs. permisivní
  licence projektu – silný copyleft a chybějící licence; informativní, ne právní rada.
- `repo-doctor sbom REPO` a export z karty repa (`b`): SBOM CycloneDX 1.5 z lockfilů, bez sítě
  (licence z cache registrů).
- Úklid místa: kontrola `disk-usage`, obrazovka **Úklid** (`9`) a `repo-doctor clean`
  (náhled / `--apply`) – jen složky ignorované gitem, přes koš (freedesktop Trash), trvalé
  smazání jen s dvojím potvrzením; nabízí i celé duplicitní klony bez změn.
- Kontrola `prepush-guard` a `repo-doctor guard`: hlídač tajemství při pushi – léčba přidá hook
  do pre-commitu (stage pre-push), nebo jen lokálně `.git/hooks/pre-push`.
- Synchronizace: obrazovka **Synchronizace** (`0`) a `repo-doctor sync` – stav rep vůči remotu
  (pozadu / napřed / rozdělené) a „Rychle dohnat“ jen `git merge --ff-only` u čistých rep,
  s náhledem a potvrzením; nikdy merge commit, rebase, reset ani push.
- Plánovaný sken: `repo-doctor schedule install|status|remove|test` a sekce v Nastavení –
  uživatelský systemd timer spouští `scan --scheduled`, který upozorní jen na změny (nové HIGH,
  nově veřejné repo s citlivým obsahem, nové zranitelné závislosti) přes `notify-send`, ntfy
  nebo `sendmail`; text bez tajemství a cest. Poslední běh ukazuje horní panel TUI.
- Léčba umí i lokální akce bez commitu (`.git/config`, hook) a varianty oprav.

### Bezpečnost
- `http://` jen pro hosty výslovně povolené v `allow_http_hosts` (hostname, `*.domena`, IP,
  CIDR) – rozhoduje se pro každý příkaz gitu zvlášť, přesměrování se nenásledují, token
  hostingu po `http` odejde jen na povolené hosty. Výchozí chování se nemění.
- Nové funkce, které mění soubory nebo repa (úklid, synchronizace, lokální léčba, plánování),
  mají vždy náhled a potvrzení s výchozí volbou „Zrušit“ a nikdy nepushují; úklid maže jen
  přes koš, synchronizace jen fast-forwardem a odmítne repo cizího vlastníka nebo s vlastními
  filtry. Všechna volání gitu dál běží s hardeningem z 0.1.4 (hooky vypnuté).
- MCP server je jen pro čtení a ve výchozím stavu offline; upozornění plánovaného skenu
  neobsahují tajemství ani cesty k souborům; ntfy po `http` jen na `allow_http_hosts`.

### Změněno
- Nové závislosti `mcp` a `pyyaml` (MCP server, čtení workflow GitHub Actions).
- Přehled jde seskupit i podle remotu (`alt+g`: triáž / složka / hosting / remote).
- Nastavení (`7`) má novou sekci *Plánovaný sken*; horní panel ukazuje poslední plánovaný běh.
- Vlastní CI workflow má akce připnuté na SHA (dogfooding `ci-security`).
- Tabulky klávesových zkratek v README se kontrolují testem proti `keymap.py`.

## [0.1.4] – 2026-10-03

### Bezpečnost
- Cizí repo už přes `.git/` ani lokální konfiguraci nespustí program při skenu, `--fetch`, `F`
  ani léčbě: hooky jsou vypnuté (`core.hooksPath=/dev/null`, i globální), fetch/ls-remote/clone
  předávají `--upload-pack=git-upload-pack` a nestahují submoduly, lokální credential helpery,
  `core.gitProxy`, `core.alternateRefsCommand`, proxy a `http.sslVerify` se neutralizují.
  Helpery a SSH nastavení z tvé globální konfigurace fungují dál.
- Síťové operace jen přes `https` a `ssh` (`GIT_ALLOW_PROTOCOL`); remote s `git://`, `http://`,
  `file://`, lokální cestou nebo `ext::` se nefetchuje a nahlásí se česky proč.

### Přidáno
- TUI: zkratka `F` spustí sken s `git fetch` (jako `repo-doctor scan --fetch`); selhání
  fetche ohlásí notifikací, přeložená SSH hláška je v kartě repa. V offline režimu se odmítne. (#15)
- Testy karty repa, reportů a okrajových větví SSH sondy a detekce cizího vlastníka. (#13, #14)

## [0.1.3] – 2026-10-02

### Opraveno
- `core.sshCommand` se najde i v souborech vložených do globální/systémové konfigurace přes
  `[include]` (`git config --includes`). Podmíněné `includeIf` podle složky se mimo repo
  nevyhodnotí – takové nastavení patří do `GIT_SSH_COMMAND` nebo `~/.ssh/config`.
  Lokální konfigurace zkoumaných rep se dál nikdy nečte.

## [0.1.2] – 2026-10-02

### Opraveno
- Git respektuje globální/systémový `core.sshCommand` (vlastní klíč, `-F` konfigurace);
  `-o BatchMode=yes` se jen přidává na konec. Pořadí: proměnná `GIT_SSH_COMMAND` →
  globální/systémový `core.sshCommand` → `ssh`. Lokální `core.sshCommand` cizího repa se
  dál nikdy nepoužije. Nová volba `ssh_batch_mode = false` pro wrappery.
- Typické SSH chyby (neznámý klíč serveru, odmítnutý SSH klíč, nedostupný server) mají
  srozumitelné české hlášky s postupem při klonu, testu připojení, `--fetch` i v CLI; původní
  text gitu zůstává v detailu.
- Repo cizího vlastníka (git: „detected dubious ownership“) už nevyrábí řadu chyb: nespustí
  se v něm žádný další příkaz gitu a skóre se nepočítá (`–`, skupina NELZE ZKONTROLOVAT).

### Přidáno
- Kontrola `unsafe-ownership` (LOW) s postupem `git config --global --add safe.directory
  <cesta>` – repo-doctor sám `safe.directory` nikdy nenastavuje.
- Test připojení hostingu při `clone_protocol = "ssh"` ověří i SSH (`git ls-remote`).
- JSON report: `score` může být `null`, nové pole `untrusted_owner`, pásmo `unchecked`.

## [0.1.1] – 2026-10-01

### Změněno
- Otisk tajemství je scrypt s pevnou solí (v2) – **allowlist položky pro tajemství je nutné
  přegenerovat** (`repo-doctor scan . --offline --report json --only secrets-history`). (#8)
- Klávesy průvodce a formulářových dialogů jsou v registru zkratek (`wizard_finish`,
  `wizard_add_forge`, `dialog_save`) – jdou přemapovat a jsou v nápovědě. (#2)
- JSON report obsahuje `finding.data`, tep je na jednom řádku; MD/HTML ukazují místní čas se zónou. (#6)

### Opraveno
- Enter v poli ukládá ve všech formulářových dialozích. (#3)
- Sloupce triáže se přizpůsobí místu v panelu při zapnutém levém panelu. (#4)
- Léčba zobrazuje celé dvouřádkové položky předpisu a na repu se změnami nic nepředvybírá. (#5)
- Lokální/neregistrové balíčky (poetry directory/git, npm `file:`/`link:`) se neposílají na OSV. (#7)
- Timeout `token_cmd` ukončí celou skupinu procesů. (#9)

## [0.1.0] – 2026-10-01

### Přidáno
- TUI (Textual) v konceptu „Triáž“: přehled s pásmy a tepem, karta repa s diagnózou,
  léčba s diffem a potvrzením, složky, hostingy, vzdálená repa, nastavení, export,
  nápověda, průvodce prvním spuštěním a příkazová paleta; světlé i tmavé téma.
- 30 kontrol v kategoriích Bezpečnost, Údržba, Stav gitu a Hosting.
- Automatické opravy (`.gitignore`, citlivé soubory, licence, pre-commit, CI, README,
  `.dockerignore`) do nové větve `repo-doctor/fixes-<datum>` bez dotyku pracovního stromu.
- Hostingy GitHub (vč. Enterprise), Gitea/Forgejo a GitLab jen pro čtení; tokeny z klíčenky,
  proměnné prostředí nebo příkazu.
- Reporty Markdown, JSON (schéma v1) a samostatné HTML; CLI `scan`, `explain`, `checks`,
  `forges test`, `config path|validate`.
- Přemapování zkratek v `[keys]` s kontrolou kolizí.
