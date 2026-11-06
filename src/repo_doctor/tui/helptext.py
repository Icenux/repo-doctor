"""Texty nápovědy v TUI: stránka pro každou obrazovku (klíče = `keymap.CONTEXTS`) a obecná část.

Zkratky se do nápovědy berou z `keymap` (jediný zdroj pravdy), tady jsou jen vysvětlující
texty. Překládají se až při zobrazení (`_()` nad hodnotami z `N_()`).
"""

from __future__ import annotations

from dataclasses import dataclass

from repo_doctor.i18n import N_


@dataclass(frozen=True)
class Page:
    """Nápověda jedné obrazovky: k čemu slouží a jak se s ní pracuje."""

    intro: str
    points: tuple[str, ...] = ()


# Obsah záložky „Vše“: (nadpis, odstavce)
GENERAL: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        N_("Co je repo-doctor"),
        (
            N_(
                "repo-doctor projde tvoje git repozitáře, najde bezpečnostní a hygienické "
                "problémy a umí bezpečně připravit opravy. Ovládá se jen z klávesnice, "
                "myš je volitelná."
            ),
        ),
    ),
    (
        N_("Typický postup"),
        (
            N_("1. Sken (R) projde všechna repa ve sledovaných složkách (1)."),
            N_(
                "2. Přehled seřadí repa podle závažnosti nálezů; ⏎ otevře kartu repa "
                "s vysvětlením a postupem u každého nálezu."
            ),
            N_("3. Léčba (f) připraví opravy do nové větve; nic se neposílá ven."),
            N_(
                "4. Na kartě repa pošleš opravnou větev (push), založíš PR/MR a po zelené CI "
                "ho mergneš – každý krok zvlášť a po potvrzení."
            ),
            N_("5. Úklid (6) a synchronizace (7) se starají o místo na disku a o aktuální repa."),
        ),
    ),
    (
        N_("Obrazovky"),
        (
            N_("1 složky s repy · 2 hostingy (GitHub, Forgejo…) · 3 vzdálená repa"),
            N_("4 nastavení · 5 export reportu · 6 úklid místa · 7 synchronizace · 8 nástroje"),
            N_(
                "Přehled (triáž) je domovská obrazovka, kam se vrátíš přes q nebo Esc. "
                "Na každé obrazovce ukáže ? nápovědu jen k ní."
            ),
        ),
    ),
    (
        N_("Pojmy"),
        (
            N_("▲ HIGH / ◆ MED / ● LOW – závažnost nálezu; skóre repa je z nich odvozené."),
            N_(
                "Allowlist (a na kartě) skryje nález, který bereš jako záměr; "
                "odebrat ho jde v Nastavení (4)."
            ),
            N_(
                "Offline režim (O) vypne síťové kontroly (OSV, registry, API hostingů); "
                "nový sken spustí R."
            ),
            N_("Koš – úklid (6) přesouvá do koše, trvale maže jen výslovné D po potvrzení."),
            N_("Synchronizace (7) dohání jen fast-forward, nikdy nepřepisuje lokální práci."),
        ),
    ),
    (
        N_("Bezpečnost"),
        (
            N_(
                "Skenování jen čte. Co mění soubory, repa nebo hosting, má náhled "
                "a potvrzení s výchozí volbou „Zrušit“. Push nikdy nejde silou "
                "(force), merge na hostingu jen po zelené CI."
            ),
        ),
    ),
    (
        N_("Nastavení a tipy"),
        (
            N_(
                "Zkratky jdou přemapovat v config.toml, sekce [keys] "
                '(akce = "klávesa"); Ctrl+P otevře příkazovou paletu '
                "se všemi akcemi a hledáním repa podle názvu."
            ),
            N_(
                "Esc = zpět / zavřít dialog · ⏎ = otevřít nebo potvrdit · ctrl+s = uložit formulář."
            ),
        ),
    ),
)

PAGES: dict[str, Page] = {
    "dashboard": Page(
        N_(
            "Přehled všech repozitářů seřazených podle závažnosti nálezů (triáž). "
            "Odtud se začíná i končí práce."
        ),
        (
            N_("R spustí sken všech repozitářů, r jen vybraného; x běžící sken zruší."),
            N_("⏎ otevře kartu repa s nálezy, f rovnou léčbu (opravy) vybraného repa."),
            N_(
                "/ hledá repo podle názvu, p nechá jen problémová repa, alt+1/2/3 filtruje závažnost."
            ),
            N_("z sbalí nebo rozbalí skupinu, alt+g přepíná seskupení (triáž / složka / hosting)."),
            N_("S řadí podle sloupce, s obrátí řazení."),
            N_(
                "Blok Notifikace v levém panelu ukazuje průběh a výsledky léčby s odesláním; "
                "⏎ otevře stránku Odeslání daného repa, N označí vše jako přečtené."
            ),
        ),
    ),
    "detail": Page(
        N_("Karta repa: vlevo nálezy podle kategorií, vpravo vysvětlení vybraného nálezu."),
        (
            N_("j/k vybírají nález; vpravo je proč to vadí a postup, jak ho opravit."),
            N_("✓ u nálezu znamená, že ho umí opravit léčba (f)."),
            N_(
                "a skryje nález do allowlistu, o otevře soubor v editoru, w repo na webu, y zkopíruje cestu."
            ),
            N_("p otevře stránku Odeslání oprav: push, PR/MR, merge a stav CI opravné větve."),
        ),
    ),
    "publish": Page(
        N_(
            "Odeslání oprav: kde je opravná větev na cestě ven – push, PR/MR, CI a merge na jednom místě."
        ),
        (
            N_(
                "P pošle větev, C vytvoří PR/MR, M ho mergne – každý krok zvlášť a s potvrzením (výchozí Zrušit)."
            ),
            N_(
                "Merge jde jen po doběhnuté zelené CI; dokud běží, stránka se sama obnovuje, ctrl+r načte stav hned."
            ),
            N_("L posune výchozí větev lokálně (jen fast-forward), U ji pak pošle na remote."),
            N_(
                "v přepne tabulku kontrol na průběh CI: graf jobů jako na GitHub Actions "
                "(✓ prošel, ✗ selhal, točící se kolečko běží, ○ čeká, ⊘ přeskočen)."
            ),
            N_(
                "V průběhu šipky nebo j/k vybírají job, ⏎ rozbalí jeho kroky, w otevře job na webu; "
                "u tabulky ⏎ otevře kontrolu."
            ),
            N_("w mimo průběh otevře PR/MR na webu."),
            N_(
                "A dotáhne větev na pozadí: chybějící push a PR, čekání na CI a po zelené CI merge. "
                "Při chybě CI se zastaví a upozorní v Notifikacích."
            ),
        ),
    ),
    "fix": Page(
        N_("Léčba: výběr oprav a náhled změn dřív, než se cokoli stane."),
        (
            N_("space vybere nebo zruší opravu, A vybere vše; vpravo je náhled změn (diff)."),
            N_("⏎ provede vybrané až po potvrzení. Změny vzniknou jen v nové větvi."),
            N_("Nic se nepushuje – odeslání je samostatný krok na kartě repa."),
            N_(
                "P vyléčí vybrané a na pozadí dotáhne push, PR, čekání na CI a po zelené CI merge; "
                "průběh je v Notifikacích na přehledu."
            ),
        ),
    ),
    "folders": Page(
        N_("Složky, ve kterých repo-doctor hledá repozitáře."),
        (
            N_("n přidá složku, e ji upraví, d odebere (soubory na disku se nemažou)."),
            N_("space složku dočasně vypne nebo zapne; po změně spusť nový sken (R)."),
        ),
    ),
    "forges": Page(
        N_(
            "Hostingy (GitHub, GitLab, Codeberg, Forgejo…), ze kterých se čtou data o CI, PR a alertech."
        ),
        (
            N_("n přidá hosting, e ho upraví, d odebere."),
            N_(
                "T otestuje připojení; výpadek hostingu nic neblokuje, kontroly se označí jako nedostupné."
            ),
            N_(
                "Token se ukládá podle zvoleného zdroje (proměnná, příkaz, keyring), nikdy do reportů."
            ),
        ),
    ),
    "remote": Page(
        N_("Repozitáře na hostingu, které nemáš lokálně, a naopak."),
        (
            N_("v přepíná pohled „chybí lokálně“ ↔ „jen lokálně“."),
            N_(
                "c naklonuje vybrané repo po potvrzení do vybrané složky; existující složku nikdy nepřepíše."
            ),
            N_("ctrl+r načte seznam znovu."),
        ),
    ),
    "settings": Page(
        N_("Nastavení aplikace; změny se ukládají okamžitě."),
        (
            N_("Plánovaný sken (denně / týdně) a test upozornění."),
            N_("Sken po spuštění, jazyk a vzhled aplikace."),
            N_("Seznam skrytých nálezů (allowlist): d vybranou položku odebere."),
        ),
    ),
    "tools": Page(
        N_("Volitelné externí nástroje (např. gitleaks), které kontroly umí využít."),
        (
            N_("i nainstaluje vybraný nástroj, I všechny chybějící."),
            N_("u aktualizuje stažené nástroje, d vybraný odebere."),
        ),
    ),
    "export": Page(
        N_("Export výsledku skenu do souboru."),
        (
            N_("Vyber formát (Markdown, JSON, HTML) a cestu, e report uloží."),
            N_("JSON má stabilní, verzované schéma pro skripty a CI."),
        ),
    ),
    "cleanup": Page(
        N_("Úklid místa: regenerovatelné složky (node_modules, .venv, target…) v repech."),
        (
            N_("space vybere repo, A vybere vše nebo nic; vidíš, kolik místa se uvolní."),
            N_("⏎ přesune vybrané do koše po potvrzení (jde vrátit)."),
            N_("D smaže trvale – jen po výslovném potvrzení."),
        ),
    ),
    "sync": Page(
        N_("Synchronizace: dohnání repozitářů za jejich remote."),
        (
            N_("f stáhne novinky (fetch) a ukáže, která repa jsou pozadu."),
            N_("space vybere repo, ⏎ dožene vybraná po potvrzení."),
            N_(
                "Dohání se jen fast-forward; repa s lokálními změnami nebo rozcházející se větví se přeskočí."
            ),
        ),
    ),
    "wizard": Page(
        N_("Průvodce prvním spuštěním: výběr složek a případně hostingu."),
        (
            N_("Vybrané složky se uloží do konfigurace a hned se naskenují."),
            N_("ctrl+s uloží a spustí sken, ctrl+n přidá hosting, Esc průvodce přeskočí."),
        ),
    ),
    "dialog": Page(
        N_("Formulářový dialog (složka, hosting, klon)."),
        (
            N_("tab přechází mezi poli; v poli cesty přijme návrh."),
            N_("ctrl+s formulář uloží, Esc ho zavře bez uložení."),
        ),
    ),
}
