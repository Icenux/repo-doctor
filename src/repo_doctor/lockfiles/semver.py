"""Minimální npm semver: ověří, že verze splňuje rozsah z `package.json` / lockfilu.

Umí běžné zápisy (`1.2.3`, `^`, `~`, `>=`, `x`, `*`, `a - b`, `||`, mezery jako AND).
Cokoli jiného (tagy, `npm:`, git, URL, předvydání v rozsahu) vyhodí `Unsupported` –
volající pak raději neaktualizuje, než aby hádal.
"""

from __future__ import annotations

import re

Version = tuple[int, int, int]


class Unsupported(Exception):
    """Zápis rozsahu (nebo verze), kterému nerozumíme."""


_PARTIAL = re.compile(r"^[vV=]?\s*(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?$")
_RELEASE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?$")


def parse_release(text: str) -> Version | None:
    """`1.2.3` → (1, 2, 3); předvydání a nesmysly → None."""
    m = _RELEASE.match(text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _partial(text: str) -> list[int | None]:
    m = _PARTIAL.match(text.strip())
    if not m:
        raise Unsupported(text)
    parts: list[int | None] = []
    for g in m.groups():
        if g is None or g in "xX*":
            parts.append(None)
        else:
            parts.append(int(g))
    # za první zástupnou hodnotou je vše zástupné
    for i, p in enumerate(parts):
        if p is None:
            parts[i:] = [None] * (3 - i)
            break
    return parts


def _fill(parts: list[int | None]) -> Version:
    return (parts[0] or 0, parts[1] or 0, parts[2] or 0)


def _bump(parts: list[int | None], index: int) -> Version:
    base = [p or 0 for p in parts]
    base[index] += 1
    for i in range(index + 1, 3):
        base[i] = 0
    return (base[0], base[1], base[2])


def _specified(parts: list[int | None]) -> int:
    return sum(1 for p in parts if p is not None)


def _comparator(token: str) -> tuple[Version | None, Version | None]:
    """Vrací [dolní, horní) hranici; `None` = bez omezení."""
    m = re.match(r"^(\^|~>?|>=|<=|>|<|=)?\s*(.*)$", token)
    op, rest = (m.group(1) or "", m.group(2)) if m else ("", token)
    if "-" in rest.split("+")[0]:
        raise Unsupported(token)  # předvydání v rozsahu
    parts = _partial(rest)
    n = _specified(parts)
    if n == 0:
        return (None, None) if op in ("", "=", "^", "~", "~>", ">=", "<=") else _never(op)
    lo, hi = _fill(parts), _bump(parts, n - 1)
    if op in ("", "="):
        return lo, hi
    if op == "^":
        index = next((i for i in range(n) if parts[i]), n - 1)
        return lo, _bump(parts, index)
    if op in ("~", "~>"):
        return lo, _bump(parts, 1 if n >= 2 else 0)
    if op == ">=":
        return lo, None
    if op == ">":
        return hi, None
    if op == "<":
        return None, lo
    return None, hi  # "<="


def _never(op: str) -> tuple[Version | None, Version | None]:
    # `>*` a `<*` nesplní žádná verze
    return (1 << 60, 0, 0), (0, 0, 0)


def _alternative(text: str) -> list[tuple[Version | None, Version | None]]:
    text = text.strip()
    if " - " in text:
        left, right = text.split(" - ", 1)
        lo = _comparator(">=" + left.strip())[0]
        hi_parts = _partial(right)
        hi = _bump(hi_parts, _specified(hi_parts) - 1) if _specified(hi_parts) else None
        return [(lo, hi)]
    tokens = re.sub(r"(\^|~>?|>=|<=|>|<|=)\s+", r"\1", text).split()
    return [_comparator(t) for t in tokens] or [(None, None)]


def satisfies(version: str, spec: str) -> bool:
    """Splňuje (nepředvydaná) verze rozsah? `Unsupported`, pokud rozsahu nerozumíme."""
    v = parse_release(version)
    if v is None:
        raise Unsupported(version)
    for alternative in spec.split("||"):
        bounds = _alternative(alternative)
        if all((lo is None or v >= lo) and (hi is None or v < hi) for lo, hi in bounds):
            return True
    return False
