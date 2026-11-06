"""Úložiště notifikací workflow léčby (#70)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from repo_doctor import inbox
from repo_doctor.inbox import Inbox
from repo_doctor.masking import register_secret
from tests.factory import fake_aws_key


def box(tmp_path: Path) -> Inbox:
    return Inbox.load(tmp_path / "data" / "inbox.json")


def test_entries_survive_restart_newest_first(tmp_path: Path) -> None:
    b = box(tmp_path)
    first = b.add("/r/a", "a", "pr", "info", "PR #1 vytvořen", "https://github.com/x/a/pull/1")
    second = b.add("/r/b", "b", "ci_failed", "error", "CI selhala")
    again = box(tmp_path)
    assert [e.id for e in again.entries] == [second.id, first.id]
    assert again.entries[1].url == "https://github.com/x/a/pull/1"
    assert again.unread() == 2


def test_mark_read_one_or_all(tmp_path: Path) -> None:
    b = box(tmp_path)
    a = b.add("/r/a", "a", "pr", "info", "x")
    b.add("/r/b", "b", "pr", "info", "y")
    b.mark_read(a.id)
    assert b.unread() == 1 and box(tmp_path).unread() == 1
    b.mark_read()
    assert b.unread() == 0 and box(tmp_path).unread() == 0
    assert b.get(a.id) is not None and b.get("nic") is None


def test_list_is_limited(tmp_path: Path) -> None:
    b = box(tmp_path)
    for i in range(inbox.LIMIT + 7):
        b.add("/r", "r", "pr", "info", f"n{i}")
    assert len(b.entries) == inbox.LIMIT
    assert b.entries[0].text == f"n{inbox.LIMIT + 6}"
    assert len(box(tmp_path).entries) == inbox.LIMIT


def test_secrets_are_masked_and_only_web_links_kept(tmp_path: Path) -> None:
    secret = fake_aws_key()
    register_secret(secret)  # token hostingu je v režimu aplikace vždy zaregistrovaný
    b = box(tmp_path)
    e = b.add("/r", "r", "pr", "weird", f"klíč {secret}", "javascript:alert(1)")
    assert secret not in e.text and secret not in (tmp_path / "data" / "inbox.json").read_text()
    assert e.url == "" and e.level == "info"


def test_file_is_private_and_corrupt_file_is_ignored(tmp_path: Path) -> None:
    b = box(tmp_path)
    b.add("/r", "r", "pr", "info", "x")
    assert stat.S_IMODE(b.file.stat().st_mode) == 0o600
    b.file.write_text("{ rozbité")
    assert box(tmp_path).entries == []
    b.file.write_text(json.dumps([{"neznámé": 1}]))
    assert box(tmp_path).entries == []
