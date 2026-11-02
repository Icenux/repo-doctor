"""Slova podle čísla (1 repo, 2 repa, 5 rep) – česky i anglicky (1 repo, 2 repos)."""

from __future__ import annotations

from repo_doctor.i18n import _, language


def plural(n: int, one: str, few: str, many: str) -> str:
    """Tvar podle čísla: 1 → `one`, 2–4 → `few`, jinak `many` (včetně 0).

    Anglicky jen 1 → `one`, jinak `many` (`few` a `many` mají stejný překlad).
    """
    if n == 1:
        return one
    if 2 <= n <= 4 and language() == "cs":
        return few
    return many


def count(n: int, one: str, few: str, many: str) -> str:
    """`5 rep`, `1 položka`…"""
    return f"{n} {plural(n, one, few, many)}"


def items(n: int) -> str:
    return count(n, _("položka"), _("položky"), _("položek"))


def repos(n: int) -> str:
    return count(n, _("repo"), _("repa"), _("rep"))


def repositories(n: int) -> str:
    """Delší tvar než `repos` – do hlaviček reportů."""
    return count(n, _("repozitář"), _("repozitáře"), _("repozitářů"))


def commits(n: int) -> str:
    return count(n, _("commit"), _("commity"), _("commitů"))


def checks(n: int) -> str:
    return count(n, _("kontrola"), _("kontroly"), _("kontrol"))


def files(n: int) -> str:
    return count(n, _("soubor"), _("soubory"), _("souborů"))


def more(n: int) -> str:
    """`… a 1 další`, `… a 5 dalších`."""
    return _("… a {n} {dalsi}").format(n=n, dalsi=plural(n, _("další"), _("další"), _("dalších")))
