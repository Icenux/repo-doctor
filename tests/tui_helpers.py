from __future__ import annotations

import asyncio
import html
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from repo_doctor import tools
from repo_doctor.config import Config, ConfigStore, RootConfig
from repo_doctor.discovery import DiscoveredRepo
from repo_doctor.models import RepoResult, ScanResult
from repo_doctor.scanner import ScanEvent, ScanOptions
from repo_doctor.tui.app import RepoDoctorApp
from tests.sample import NOW, sample_result

CONFIG = """\
# testovací konfigurace
[[roots]]
path = "~/projekty"

[[roots]]
path = "~/git-archiv"

[[roots]]
path = "~/sandbox"
enabled = false

[[forges]]
name = "github"
type = "github"

[[forges]]
name = "forgejo"
type = "forgejo"
url = "https://git.example.ts.net:3000"
"""


class FakeTools:
    """Náhrada `repo_doctor.tools` pro TUI: deterministický stav, nic nespouští ani nestahuje."""

    ToolsError = tools.ToolsError

    def __init__(
        self,
        *,
        missing: tuple[str, ...] = (),
        manager: str | None = "apt",
        downloaded: dict[str, str | None] | None = None,  # id → verze stažené binárky
    ) -> None:
        self.missing_ids = set(missing)
        self.manager = manager
        self.downloaded = dict(downloaded or {})
        self.commands: list[list[str]] = []
        self.downloads: list[str] = []
        self.removed: list[str] = []
        self.code = 0

    def find(self, binary: str) -> str | None:
        tool = next((t for t in tools.TOOLS if t.binary == binary), None)
        if tool and tool.id in self.missing_ids:
            return None
        if tool and tool.id in self.downloaded:
            return f"/home/u/.local/share/repo-doctor/tools/{binary}"
        return f"/usr/bin/{binary}"

    def status(self) -> list[tools.ToolStatus]:
        return [
            tools.ToolStatus(
                t, self.find(t.binary), t.id in self.downloaded, self.downloaded.get(t.id)
            )
            for t in tools.TOOLS
        ]

    def missing(self) -> list[tools.Tool]:
        return [s.tool for s in self.status() if not s.installed]

    def outdated(self) -> list[tools.Tool]:
        return [s.tool for s in self.status() if s.outdated]

    update_plan = staticmethod(tools.update_plan)

    def remove_download(self, tool: tools.Tool) -> Path:
        if tool.id not in self.downloaded:
            raise tools.ToolsError(f"{tool.id} není stažený – odinstaluj ho správcem balíčků.")
        del self.downloaded[tool.id]
        self.missing_ids.add(tool.id)
        self.removed.append(tool.id)
        return Path("/tmp/tools") / tool.binary

    def keyring_status(self) -> tuple[bool, str]:
        return True, "SecretService Keyring"

    def detect_manager(self) -> tools.PackageManager | None:
        if self.manager is None:
            return None
        has = self.manager != "apt"  # „apt“ = Debian 12 bez gitleaks v repozitáři
        return tools.PackageManager(
            self.manager,  # type: ignore[arg-type]
            runner=lambda _cmd: subprocess.CompletedProcess([], 0 if has else 1, "", ""),
        )

    plan = staticmethod(tools.plan)
    describe = staticmethod(tools.describe)

    def run_command(self, plan: tools.InstallPlan) -> int:
        if plan.command:
            self.commands.append(plan.command)
        return self.code

    def install_download(self, d: tools.Download) -> Path:
        self.downloads.append(d.name)
        self.missing_ids.discard(d.name)
        self.downloaded[d.name] = d.version
        return Path("/tmp/tools") / d.name


def make_app(
    tmp: Path,
    *,
    config: str | None = CONFIG,
    result: ScanResult | None = None,
    **kw: Any,
) -> RepoDoctorApp:
    cfg = tmp / "config.toml"
    if config is not None:
        cfg.write_text(config)
    kw.setdefault("auto_scan", False)
    kw.setdefault("tools_backend", FakeTools())
    kw.setdefault("history_delta", {"high": 2, "low": -5, "no_pulse": 1})
    return RepoDoctorApp(
        store=ConfigStore(cfg),
        initial_result=result if result is not None else sample_result(),
        clock=lambda: NOW,
        load_cache=False,
        **kw,
    )


class FakeScanner:
    """Scanner, který vydává předpřipravené výsledky postupně (pro test průběžného plnění)."""

    def __init__(
        self, repos: list[RepoResult], on_event: Callable[[ScanEvent], None], delay: float = 0.05
    ) -> None:
        self.repos = repos
        self.on_event = on_event
        self.delay = delay
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    async def run(
        self, roots: list[RootConfig] | None = None, repos: list[DiscoveredRepo] | None = None
    ) -> ScanResult:
        result = ScanResult(started_at=NOW, roots=[r.path for r in roots or []])
        self.on_event(ScanEvent("discovered", total=len(self.repos)))
        for i, r in enumerate(self.repos, start=1):
            if self.cancelled:
                break
            self.on_event(
                ScanEvent(
                    "check", total=len(self.repos), done=i - 1, repo=r.name, check="secrets-tree"
                )
            )
            await asyncio.sleep(self.delay)
            result.repos.append(r)
            self.on_event(
                ScanEvent("repo_done", total=len(self.repos), done=i, repo=r.name, result=r)
            )
        result.cancelled = self.cancelled
        result.finished_at = NOW
        self.on_event(ScanEvent("finished", total=len(self.repos), done=len(result.repos)))
        return result


def fake_factory(
    repos: list[RepoResult], delay: float = 0.05
) -> Callable[[Config, ScanOptions, Callable[[ScanEvent], None]], Any]:
    def factory(config: Config, options: ScanOptions, on_event: Callable[[ScanEvent], None]) -> Any:
        return FakeScanner(repos, on_event, delay)

    return factory


def screen_text(app: RepoDoctorApp) -> str:
    """Viditelný text obrazovky (z SVG screenshotu, bez značek)."""
    svg = app.export_screenshot()
    parts = re.findall(r"<text[^>]*>(.*?)</text>", svg, flags=re.DOTALL)
    return html.unescape("\n".join(parts)).replace("\xa0", " ")
