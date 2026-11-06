"""Plánovaný sken: uživatelský systemd timer, upozornění jen na změny, bez tajemství."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from repo_doctor import cli, netpolicy, notify, schedule
from repo_doctor.config import ConfigError, NotifyConfig, parse_config
from repo_doctor.models import Category, Finding, RepoResult, ScanResult, Severity
from tests import factory as fx
from tests.factory import RepoBuilder


class FakeSystemctl:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.ok = ok

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        out = "ActiveState=active\nNextElapseUSecRealtime=Mon 2026-10-19 08:00:00 CEST\n"
        return subprocess.CompletedProcess(
            cmd, 0 if self.ok else 1, out if "show" in cmd else "", ""
        )


@pytest.fixture
def systemd(monkeypatch: pytest.MonkeyPatch) -> FakeSystemctl:
    fake = FakeSystemctl()
    monkeypatch.setattr(schedule, "_run", fake)
    monkeypatch.setattr(schedule, "systemd_available", lambda runner=None: True)
    monkeypatch.setattr("repo_doctor.schedule.shutil.which", lambda name: f"/usr/bin/{name}")
    return fake


def test_install_status_remove_user_units(systemd: FakeSystemctl) -> None:
    files = schedule.install("daily", "07:30")
    unit_dir = schedule.unit_dir()
    assert files == [unit_dir / "repo-doctor.service", unit_dir / "repo-doctor.timer"]
    assert str(unit_dir).endswith(".config/systemd/user")
    service, timer = (f.read_text() for f in files)
    assert "ExecStart=/usr/bin/repo-doctor scan --scheduled" in service
    assert "OnCalendar=*-*-* 07:30:00" in timer and "Persistent=true" in timer
    assert ["systemctl", "--user", "enable", "--now", "repo-doctor.timer"] in systemd.calls
    assert all("--user" in c for c in systemd.calls)  # nic jako root / systémově
    st = schedule.status()
    assert st.installed and st.active and st.schedule == "*-*-* 07:30:00"
    assert st.next_run and "08:00" in st.next_run
    assert schedule.remove() is True
    assert not files[0].exists() and not files[1].exists()
    assert schedule.remove() is False


def test_weekly_and_validation(systemd: FakeSystemctl) -> None:
    assert schedule.on_calendar("weekly", "08:00") == "Mon *-*-* 08:00:00"
    with pytest.raises(schedule.ScheduleError, match="HH:MM"):
        schedule.install("weekly", "25:00")


def test_without_systemd_offers_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule, "systemd_available", lambda runner=None: False)
    with pytest.raises(schedule.ScheduleError) as exc:
        schedule.install("weekly", "08:00")
    assert "není k dispozici" in str(exc.value)
    assert "0 8 * * 1 " in str(exc.value) and "scan --scheduled" in str(exc.value)
    assert not schedule.unit_dir().exists()


# ------------------------------------------------------------------ změny a text
def _finding(check: str, sev: Severity, key: str, path: str = "config/tajne.env") -> Finding:
    return Finding(
        check_id=check, severity=sev, category=Category.SECURITY, title=check,
        message=f"zpráva s cestou {path}", key=key,
    )  # fmt: skip


def _result(**repos: list[Finding]) -> ScanResult:
    return ScanResult(
        repos=[RepoResult(path=f"/r/{n}", name=n, root="~", findings=f) for n, f in repos.items()]
    )


def test_diff_only_new_things() -> None:
    old = _result(a=[_finding("secrets-tree", Severity.HIGH, "k1")], b=[])
    assert not notify.diff(None, old)  # první běh neupozorňuje
    assert not notify.diff(old, old)
    new = _result(
        a=[
            _finding("secrets-tree", Severity.HIGH, "k1"),
            _finding("secrets-tree", Severity.HIGH, "k2"),
        ],
        b=[_finding("deps-vulnerable", Severity.HIGH, "pkg")],
    )
    new.repos[1].visibility = "public"
    new.repos[1].findings.append(_finding("public-sensitive", Severity.HIGH, "inv"))
    changes = notify.diff(old, new)
    assert changes.new_high == {"a": 1, "b": 2} and changes.new_vulns == {"b": 1}
    assert changes.newly_public == ["b"]
    text = changes.text()
    assert "3 nové HIGH" in text and "tajne.env" not in text and "/r/" not in text


# ------------------------------------------------------------------ kanály
def test_ntfy_https_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    monkeypatch.setenv("NTFY_TOKEN_TEST", "tk_" + "a" * 20)
    cfg = NotifyConfig(ntfy_url="https://ntfy.example/repo", token_env="NTFY_TOKEN_TEST")
    d = notify.send_ntfy(cfg, "repo-doctor", "2 nové HIGH", transport=httpx.MockTransport(handler))
    assert d.ok and seen[0].headers["authorization"] == "Bearer tk_" + "a" * 20
    assert seen[0].content == "2 nové HIGH".encode()


def test_ntfy_http_only_for_allowed_hosts() -> None:
    with pytest.raises(ConfigError, match="allow_http_hosts"):
        parse_config({"notify": {"ntfy_url": "http://ntfy.local/t"}})
    cfg = parse_config(
        {"allow_http_hosts": ["ntfy.local"], "notify": {"ntfy_url": "http://ntfy.local/t"}}
    )
    netpolicy.configure([])  # seznam se mezitím vyprázdnil → nic se nepošle
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200)

    d = notify.send_ntfy(
        cfg.notify,
        "t",
        "x",
        transport=httpx.MockTransport(handler),
    )
    assert not d.ok and called == []
    with pytest.raises(ConfigError, match="https"):
        parse_config({"notify": {"ntfy_url": "ftp://x"}})


def test_desktop_and_email(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "mail.txt"
    sendmail = tmp_path / "sendmail"
    sendmail.write_text(f"#!/bin/sh\ncat > '{log}'\n")
    sendmail.chmod(0o755)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not notify.send_desktop("t", "x").ok  # bez grafické relace nic
    monkeypatch.setattr(
        "repo_doctor.notify.shutil.which",
        lambda name: str(sendmail) if name == "sendmail" else None,
    )
    d = notify.send_email("ja@example.com", "repo-doctor", "1 nový HIGH nález (a)")
    assert d.ok and "To: ja@example.com" in log.read_text() and "1 nový HIGH" in log.read_text()


def test_scheduled_scan_notifies_only_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[str] = []

    def fake_deliver(cfg: NotifyConfig, text: str, **kw: Any) -> list[notify.Delivery]:
        sent.append(text)
        return [notify.Delivery("desktop", True)]

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    rb = RepoBuilder.create(tmp_path / "projekty" / "app")
    rb.write("README.md", "x").commit()
    root = str(tmp_path / "projekty")
    args = ["scan", root, "--offline", "--scheduled"]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    assert sent == []  # beze změn žádné upozornění
    secret = fx.fake_aws_key(9)
    rb.write("config/prod.env", f"AWS={secret}\n").commit("omylem")
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert len(sent) == 1 and "nové HIGH nálezy" in sent[0] and "app" in sent[0]
    assert secret not in sent[0] and "prod.env" not in sent[0] and secret not in out
    state = notify.load_state()
    assert state is not None and state.changes == sent[0] and state.deliveries[0]["ok"] is True
    assert json.loads(notify.state_file().read_text())["repos"] == 1


def test_schedule_cli(systemd: FakeSystemctl, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["schedule", "status"]) == 0
    assert "není nainstalovaný" in capsys.readouterr().out
    assert cli.main(["schedule", "install", "--interval", "daily", "--time", "06:15"]) == 0
    assert cli.main(["schedule", "status"]) == 0
    assert "aktivní" in capsys.readouterr().out
    assert cli.main(["schedule", "install", "--interval", "hourly"]) == 2
    assert cli.main(["schedule", "remove"]) == 0
    assert "odebrán" in capsys.readouterr().out


# ------------------------------------------------------------------ TUI
class FakeBackend:
    ScheduleError = schedule.ScheduleError

    def __init__(self) -> None:
        self.installed: tuple[str, str] | None = None

    def status(self) -> schedule.Status:
        if self.installed:
            interval, time = self.installed
            when = schedule.on_calendar("weekly" if interval == "weekly" else "daily", time)
            return schedule.Status(True, True, when, "Po 19. 10. 08:00")
        return schedule.Status(False)

    def install(self, interval: str, time: str) -> list[Path]:
        schedule.parse_time(time)
        self.installed = (interval, time)
        return []

    def remove(self) -> bool:
        was = self.installed is not None
        self.installed = None
        return was


@pytest.mark.parametrize("size", [(80, 24), (160, 48)], ids=["80x24", "160x48"])
def test_settings_schedule_snapshot(
    snap_compare: object, tmp_path: Path, size: tuple[int, int]
) -> None:
    from tests.tui_helpers import make_app

    async def run_before(pilot: Any) -> None:
        await pilot.press("4")
        await pilot.pause(0.3)
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    fixed = Path("/tmp/repo-doctor-snapshot")  # cesta ke konfiguraci je vidět v nastavení
    fixed.mkdir(exist_ok=True)
    app = make_app(fixed, schedule_backend=FakeBackend())
    assert snap_compare(app, terminal_size=size, run_before=run_before)  # type: ignore[operator]


async def test_settings_install_and_remove(tmp_path: Path) -> None:
    from textual.widgets import Button, Input

    from repo_doctor.tui.screens.dialogs import ConfirmDialog
    from tests.tui_helpers import make_app

    backend = FakeBackend()
    app = make_app(tmp_path, schedule_backend=backend)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause()
        settings = app.screen
        settings.query_one("#schedule-time", Input).value = "06:45"
        settings.query_one("#schedule-install", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)  # náhled souborů, výchozí Zrušit
        assert "repo-doctor.timer" in str(app.screen.query_one("#confirm-body").render())
        await pilot.press("enter")
        await pilot.pause()
        assert backend.installed is None
        settings.query_one("#schedule-install", Button).press()
        await pilot.pause()
        app.screen.query_one("#ok", Button).press()
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert backend.installed == ("weekly", "06:45")
        status = str(settings.query_one("#schedule-status").render())
        assert "zapnutý" in status and "týdně v pondělí 06:45" in status
        settings.query_one("#schedule-remove", Button).press()
        await pilot.pause()
        app.screen.query_one("#ok", Button).press()
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        assert backend.installed is None
