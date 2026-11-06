"""Notifikace workflow léčby (#70): co se s opravami stalo, kde, a kam po kliknutí.

Ukládá se do `inbox.json` v datové složce, takže přežijí restart. Zápis je atomický (dočasný soubor
+ `os.replace`) a seznam je omezený na posledních `LIMIT` položek. Text je krátký: název repa,
číslo PR a odkaz – nikdy tajemství ani cesty k souborům s nálezy (zprávy procházejí `redact`).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from repo_doctor import paths
from repo_doctor.masking import redact

LIMIT = 100
LEVELS = ("info", "ok", "warn", "error")


@dataclass
class Entry:
    id: str
    ts: str  # ISO 8601, UTC
    repo_path: str
    repo_name: str
    kind: str
    level: str
    text: str
    url: str = ""  # odkaz na PR/MR na webu
    read: bool = False


def inbox_file() -> Path:
    return paths.data_dir() / "inbox.json"


@dataclass
class Inbox:
    """Seznam notifikací, nejnovější první. Vlákna-bezpečný (zapisuje pracovní vlákno workflow)."""

    file: Path = field(default_factory=inbox_file)
    entries: list[Entry] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def load(cls, file: Path | None = None) -> Inbox:
        box = cls(file or inbox_file())
        try:
            data = json.loads(box.file.read_text("utf-8"))
            box.entries = [Entry(**item) for item in data][:LIMIT]
        except (OSError, ValueError, TypeError):
            box.entries = []
        return box

    def add(
        self,
        repo_path: str,
        repo_name: str,
        kind: str,
        level: str,
        text: str,
        url: str = "",
        *,
        now: datetime | None = None,
    ) -> Entry:
        entry = Entry(
            id=uuid.uuid4().hex[:12],
            ts=(now or datetime.now(UTC)).isoformat(),
            repo_path=repo_path,
            repo_name=redact(repo_name),
            kind=kind,
            level=level if level in LEVELS else "info",
            text=redact(text),
            url=url if url.startswith(("https://", "http://")) else "",
        )
        with self._lock:
            self.entries.insert(0, entry)
            del self.entries[LIMIT:]
            self._save()
        return entry

    def get(self, entry_id: str) -> Entry | None:
        with self._lock:
            return next((e for e in self.entries if e.id == entry_id), None)

    def unread(self) -> int:
        with self._lock:
            return sum(1 for e in self.entries if not e.read)

    def mark_read(self, entry_id: str | None = None) -> None:
        """Označí jednu notifikaci (nebo všechny) jako přečtenou."""
        with self._lock:
            changed = False
            for e in self.entries:
                if (entry_id is None or e.id == entry_id) and not e.read:
                    e.read = changed = True
            if changed:
                self._save()

    def _save(self) -> None:
        try:
            paths.ensure_private_dir(self.file.parent)
            tmp = self.file.with_name(self.file.name + f".{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps([asdict(e) for e in self.entries], ensure_ascii=False), "utf-8"
            )
            tmp.chmod(0o600)
            tmp.replace(self.file)
        except OSError:
            pass  # notifikace jsou pohodlí; nezapsání nesmí shodit workflow
