"""Vývojářské nástroje k překladům: vytažení textů z kódu a synchronizace `.po`.

    python -m repo_doctor.i18n_tools          # doplní nové texty do locale/en.po
    python -m repo_doctor.i18n_tools --check  # jen ověří (exit 1, když něco chybí)

Hledá volání `_("…")` a `N_("…")` s doslovným řetězcem v `src/repo_doctor`. Nové texty
přidá s prázdným překladem, nepoužívané odebere, existující překlady zachová.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

from repo_doctor.i18n import LOCALE_DIR, parse_po

PACKAGE = Path(__file__).parent
FUNCS = {"_", "N_"}


def extract(files: Iterable[Path] | None = None) -> dict[str, list[str]]:
    """{msgid: [soubor:řádek, …]} ze všech volání `_()` / `N_()` s doslovným řetězcem."""
    found: dict[str, list[str]] = {}
    for path in sorted(files if files is not None else PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in FUNCS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                where = f"{path.relative_to(PACKAGE.parent)}:{node.lineno}"
                found.setdefault(node.args[0].value, []).append(where)
    return found


def _quote(text: str) -> str:
    body = text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    if "\n" not in body:
        return f'"{body}"'
    parts = body.split("\n")
    lines = [f"{p}\\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    return '""\n' + "\n".join(f'"{p}"' for p in lines)


def render_po(lang: str, messages: dict[str, list[str]], translations: dict[str, str]) -> str:
    out = [
        "# Překlad repo-doctoru. Zdrojový jazyk je čeština (msgid).",
        "# Po změně textů v kódu: python -m repo_doctor.i18n_tools",
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        f'"Language: {lang}\\n"',
        "",
    ]
    for msgid in sorted(messages):
        out.append("#: " + " ".join(messages[msgid][:3]))
        out.append(f"msgid {_quote(msgid)}")
        out.append(f"msgstr {_quote(translations.get(msgid, ''))}")
        out.append("")
    return "\n".join(out)


def missing(lang: str = "en") -> tuple[list[str], list[str]]:
    """(texty bez překladu, překlady, které se v kódu už nepoužívají)."""
    messages = extract()
    po = LOCALE_DIR / f"{lang}.po"
    translations = parse_po(po.read_text("utf-8")) if po.exists() else {}
    return (
        sorted(m for m in messages if not translations.get(m)),
        sorted(m for m in translations if m not in messages),
    )


def update(lang: str = "en") -> Path:
    po = LOCALE_DIR / f"{lang}.po"
    translations = parse_po(po.read_text("utf-8")) if po.exists() else {}
    LOCALE_DIR.mkdir(exist_ok=True)
    po.write_text(render_po(lang, extract(), translations), "utf-8")
    return po


if __name__ == "__main__":  # pragma: no cover
    if "--check" in sys.argv:
        todo, obsolete = missing()
        for m in todo:
            print(f"chybí překlad: {m!r}")
        for m in obsolete:
            print(f"nepoužívaný překlad: {m!r}")
        sys.exit(1 if todo or obsolete else 0)
    path = update()
    todo, _obsolete = missing()
    print(f"{path}: {len(todo)} textů bez překladu")
