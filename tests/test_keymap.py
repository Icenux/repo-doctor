from __future__ import annotations

from pathlib import Path

import pytest

from repo_doctor.i18n import catalog
from repo_doctor.keymap import (
    ACTIONS,
    KeymapError,
    build_keymap,
    display_key,
    grouped,
    is_valid_key,
    migrate_overrides,
    normalize_key,
)


def test_defaults_have_no_collisions() -> None:
    km = build_keymap({})
    assert km["scan_all"] == "R"
    assert len(km) == len(ACTIONS)


def test_remap_ok() -> None:
    km = build_keymap({"scan_all": "F5", "open_fix": "ctrl+f"})
    assert km["scan_all"] == "f5"
    assert km["open_fix"] == "ctrl+f"


def test_every_function_has_one_action() -> None:
    """Jedna funkce = jedna akce = jedna klávesa (#32)."""
    actions = [a.action for a in ACTIONS]
    assert len(actions) == len(set(actions))
    descriptions = [(a.context, a.description) for a in ACTIONS]
    assert len(descriptions) == len(set(descriptions))


def test_legacy_ids_still_work() -> None:
    assert migrate_overrides({"dash_fix": "F3", "screen_overview": "F1"}) == {"open_fix": "F3"}
    assert migrate_overrides({"detail_fix": "F3", "open_fix": "F4"}) == {"open_fix": "F4"}
    assert migrate_overrides({"screen_detail": "F2"}) == {"open_card": "F2"}
    km = build_keymap({"detail_fix": "ctrl+f", "screen_overview": "F1"})
    assert km["open_fix"] == "ctrl+f"


def test_open_fix_conflicts_in_both_contexts() -> None:
    with pytest.raises(KeymapError, match="kolize"):
        build_keymap({"open_fix": "o"})  # o = editor v kartě repa
    with pytest.raises(KeymapError, match="kolize"):
        build_keymap({"open_fix": "z"})  # z = sbalit na přehledu


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"nope": "x"}, "neznámá akce"),
        ({"scan_all": "ctrl+"}, "neplatná klávesa"),
        ({"scan_all": "foo"}, "neplatná klávesa"),
        ({"scan_all": "esc"}, "vyhrazená"),
        ({"scan_all": "j"}, "kolize"),  # s pohybem
        ({"allowlist": "o"}, "kolize"),  # v rámci karty
        ({"clone": "t"}, "kolize"),  # s globální
    ],
)
def test_invalid(overrides: dict[str, str], needle: str) -> None:
    with pytest.raises(KeymapError) as exc:
        build_keymap(overrides)
    assert needle in str(exc.value)


def test_same_key_in_different_contexts_ok() -> None:
    build_keymap({"clone": "e"})  # e = export i upravit jinde, ale jiný kontext


def test_helpers() -> None:
    assert normalize_key("?") == "question_mark"
    assert normalize_key("Ctrl+Alt+X") == "alt+ctrl+X"
    assert display_key("question_mark") == "?"
    assert is_valid_key("alt+1") and is_valid_key("f12") and not is_valid_key("+")
    g = grouped()
    assert ("?", "nápověda") in g["Globální"]
    assert ("R", "skenovat všechna repa") in g["Globální"]
    g2 = grouped({**build_keymap({"scan_all": "F5"})})
    assert ("f5", "skenovat všechna repa") in g2["Globální"]


def test_readme_key_tables_match_keymap() -> None:
    readme = (Path(__file__).parent.parent / "README.md").read_text("utf-8")
    en = catalog("en")  # README je anglicky, popisy zkratek se berou z locale/en.po
    for ctx, rows in grouped().items():
        assert f"**{en.get(ctx, ctx)}**" in readme
        for key, desc in rows:
            assert f"| `{key}` | {en.get(desc, desc)} |" in readme, (ctx, key)
