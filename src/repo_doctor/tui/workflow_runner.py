"""Spouštění workflow léčby (#70) na pozadí aplikace a zápis jeho událostí do notifikací.

Workflow běží v pracovním vlákně aplikace (ne obrazovky), takže přežije přechod mezi stránkami;
zavřením aplikace se zastaví (`stop_all`) a opravná větev i PR zůstanou k ručnímu dokončení.
Jedno repo = nejvýš jedno běžící workflow.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from datetime import date
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from repo_doctor import notify, workflow
from repo_doctor.gitwrap import Git, GitError
from repo_doctor.i18n import _
from repo_doctor.inbox import Entry, Inbox
from repo_doctor.models import Patch

if TYPE_CHECKING:
    from repo_doctor.tui.app import RepoDoctorApp


class WorkflowRunner:
    def __init__(self, app: RepoDoctorApp, inbox: Inbox | None = None) -> None:
        self.app = app
        self.inbox = inbox or Inbox.load()
        self.live: dict[str, str] = {}  # cesta repa → co workflow právě dělá
        self._cancels: dict[str, threading.Event] = {}
        self.sleep: workflow.Sleep | None = None  # testy podstrčí okamžité čekání
        self.timings: dict[str, float] = {}  # testy: poll / grace / timeout

    # ------------------------------------------------------------------ dotazy
    def active(self, path: str) -> bool:
        return path in self.live

    def repo_name(self, path: str) -> str:
        repo = self.app.repo(path)
        return repo.name if repo else Path(path).name

    # ------------------------------------------------------------------ spuštění
    def start_heal(self, path: str, patches: list[Patch]) -> bool:
        today = self.app.now().date() if self.app.now() else date.today()
        return self._start(
            path,
            lambda git, ctx: workflow.heal_and_publish(
                git, self.app.config, patches, ctx, today=today
            ),
        )

    def start_publish(self, path: str, branch: str | None = None) -> bool:
        return self._start(
            path, lambda git, ctx: workflow.publish_existing(git, self.app.config, ctx, branch)
        )

    def _start(self, path: str, job: Callable[[Git, workflow.Context], None]) -> bool:
        if self.active(path):
            self.app.notify(
                _("Workflow tohoto repa už běží – průběh je v Notifikacích na přehledu."),
                severity="warning",
            )
            return False
        if self.app.offline:
            self.app.notify(
                _("Workflow potřebuje hosting – vypni offline režim ({key}).").format(
                    key=self.app.key("toggle_offline")
                ),
                severity="warning",
            )
            return False
        cancel = threading.Event()
        self._cancels[path] = cancel
        self.live[path] = _("startuje…")
        ctx = workflow.Context(
            emit=partial(self._emit, path),
            transport=self.app.forge_transport,
            cancel=cancel,
            sleep=self.sleep,
            **self.timings,
        )
        self.app.run_worker(
            partial(self._thread, path, job, ctx),
            thread=True,
            group=f"workflow:{path}",
            name=f"workflow:{path}",
        )
        self.app.on_inbox_changed()
        return True

    def stop_all(self) -> None:
        for cancel in self._cancels.values():
            cancel.set()

    # ------------------------------------------------------------------ vlákno
    def _thread(
        self, path: str, job: Callable[[Git, workflow.Context], None], ctx: workflow.Context
    ) -> None:
        final = False
        inner = ctx.emit

        def emit(event: workflow.Event) -> None:
            nonlocal final
            final = final or event.final
            inner(event)

        ctx.emit = emit  # sleduje, jestli workflow skončilo událostí
        try:
            job(Git(Path(path)), ctx)
        except GitError as err:
            ctx.emit(
                workflow.Event("refused", "error", _("git selhal: {err}").format(err=err.stderr))
            )
        except Exception as err:  # workflow nesmí skončit potichu
            ctx.emit(
                workflow.Event(
                    "refused",
                    "error",
                    _("Workflow selhalo nečekanou chybou: {err}").format(
                        err=f"{type(err).__name__}: {err}"
                    ),
                )
            )
        else:
            if not final:  # pragma: no cover - každá větev workflow končí událostí
                ctx.emit(workflow.Event("refused", "warn", _("Workflow skončilo bez výsledku.")))

    def _emit(self, path: str, event: workflow.Event) -> None:
        """Volá se z pracovního vlákna."""
        name = self.repo_name(path)
        entry: Entry | None = None
        if event.stored:
            entry = self.inbox.add(path, name, event.kind, event.level, event.text, event.url)
        if event.final and event.level in ("error", "warn") and self.app.config.notify.desktop:
            notify.send_desktop("repo-doctor", f"{name}: {event.text}")
        with contextlib.suppress(RuntimeError):  # aplikace se mezitím zavřela
            self.app.call_from_thread(self._ui, path, name, event, entry)

    # ------------------------------------------------------------------ hlavní vlákno
    def _ui(self, path: str, name: str, event: workflow.Event, entry: Entry | None) -> None:
        if event.final:
            self.live.pop(path, None)
            self._cancels.pop(path, None)
        else:
            self.live[path] = event.text
        if entry is not None:
            severity = {"error": "error", "warn": "warning"}.get(event.level, "information")
            self.app.notify(
                event.text,
                title=name,
                severity=severity,  # type: ignore[arg-type]
                timeout=20 if event.final else 8,
            )
        self.app.on_inbox_changed()
