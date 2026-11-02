"""Závislosti: zranitelnosti (OSV.dev), zastaralé verze (PyPI, npm), chybějící lockfile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from repo_doctor import depupdate
from repo_doctor.checks.base import Check, RepoContext, SkipCheck, register
from repo_doctor.deps import fixed_in
from repo_doctor.i18n import N_, _
from repo_doctor.models import Category, FileChange, Finding, Patch, Severity

if TYPE_CHECKING:
    from repo_doctor.deps import DepsData


def _require_network(repo: RepoContext) -> DepsData:
    if repo.offline:
        raise SkipCheck(_("offline režim"))
    if repo.deps is None or not repo.deps.network_done:
        raise SkipCheck(repo.deps_status or _("síťová data nejsou k dispozici"))
    return repo.deps


@register
class DepsVulnerable(Check):
    id = "deps-vulnerable"
    title = N_("Zranitelné závislosti")
    severity = Severity.HIGH
    category = Category.SECURITY
    network = True

    def run(self, repo: RepoContext) -> list[Finding]:
        deps = _require_network(repo)
        findings = []
        for dep, vulns in sorted(deps.vulns.items(), key=lambda kv: (kv[0].ecosystem, kv[0].name)):
            worst = max((v.severity for v in vulns), key=lambda s: s.rank)
            ids = ", ".join(v.id for v in vulns[:6]) + (
                f" (+{len(vulns) - 6})" if len(vulns) > 6 else ""
            )
            fixed = fixed_in(dep, vulns)
            can_fix = (
                fixed is not None
                and depupdate.valid_target(dep.name, fixed)
                and depupdate.tool_for(dep.source) is not None
            )
            findings.append(
                self.finding(
                    f"{dep.name} {dep.version} ({dep.ecosystem}): {ids}",
                    severity=worst,
                    path=dep.source,
                    key=f"{dep.ecosystem}:{dep.name}:{dep.version}",
                    fixable=can_fix,
                    vulns=[v.id for v in vulns],
                    package=dep.name,
                    version=dep.version,
                    fixed_in=fixed or "",
                )
            )
        return findings

    def fix(self, repo: RepoContext, findings: list[Finding]) -> Patch | None:
        """Aktualizuje zranitelné závislosti v lockfilech (správce balíčků v dočasné složce)."""
        head = repo.git.head_sha()
        if head is None:
            return None
        grouped: dict[str, list[depupdate.Target]] = {}
        for f in findings:
            lock, name, version, fixed = (
                f.location.path,
                f.data.get("package"),
                f.data.get("version"),
                f.data.get("fixed_in"),
            )
            if not (
                lock
                and isinstance(name, str)
                and isinstance(version, str)
                and isinstance(fixed, str)
            ):
                continue
            grouped.setdefault(lock, []).append(depupdate.Target(name, version, fixed))
        changes: list[FileChange] = []
        notes: list[str] = []
        upgraded: list[str] = []
        for lock, targets in sorted(grouped.items()):
            update = depupdate.update_lock(repo.git, head, lock, targets)
            if update.error:
                notes.append(
                    _("{lock}: aktualizace se nepovedla ({error})").format(
                        lock=lock, error=update.error
                    )
                )
                continue
            for name, (old, new) in sorted(update.upgraded.items()):
                upgraded.append(f"{name} {old}→{new}")
            notes.extend(
                _(
                    "{lock}: {dep} zůstává na zranitelné verzi (omezení v manifestu nebo jiné závislosti) – uprav ručně nebo přidej do allowlistu"
                ).format(lock=lock, dep=dep)
                for dep in update.unresolved
            )
            if update.content is not None:
                changes.append(
                    FileChange(path=lock, action="replace", content=update.content, old=update.old)
                )
        if not changes:
            return None
        notes.append(_("Po mergi spusť testy projektu – aktualizace mohou změnit chování."))
        return Patch(
            check_id=self.id,
            title=_("Aktualizovat zranitelné závislosti"),
            summary=", ".join(upgraded),
            changes=changes,
            notes=notes,
        )


@register
class DepsOutdated(Check):
    id = "deps-outdated"
    title = N_("Zastaralé závislosti")
    severity = Severity.LOW
    category = Category.MAINTENANCE
    network = True

    def run(self, repo: RepoContext) -> list[Finding]:
        deps = _require_network(repo)
        findings = []
        for level, sev in (
            ("major", Severity.MEDIUM),
            ("minor", Severity.LOW),
            ("patch", Severity.LOW),
        ):
            items = [o for o in deps.outdated if o.level == level]
            if not items:
                continue
            listing = ", ".join(f"{o.dep.name} {o.dep.version}→{o.latest}" for o in items[:8])
            more = _(" a {count} další").format(count=len(items) - 8) if len(items) > 8 else ""
            findings.append(
                self.finding(
                    f"{len(items)}× {level}: {listing}{more}",
                    title=_("Zastaralé závislosti ({level})").format(level=level),
                    severity=sev,
                    key=level,
                    packages=[f"{o.dep.name}=={o.dep.version}->{o.latest}" for o in items],
                )
            )
        return findings


@register
class DepsLockfileMissing(Check):
    id = "deps-lockfile-missing"
    title = N_("Chybí lockfile")
    severity = Severity.MEDIUM
    category = Category.SECURITY

    def run(self, repo: RepoContext) -> list[Finding]:
        from repo_doctor.deps import collect

        data = repo.deps or collect(repo.tracked_files, repo.read_text)
        return [
            self.finding(
                _(
                    "{manifest} deklaruje závislosti, ale chybí {lock} – verze nejsou připnuté a zranitelnosti nejde ověřit"
                ).format(manifest=manifest, lock=lock),
                path=manifest,
                key=manifest,
            )
            for manifest, lock in data.missing_locks
        ]
