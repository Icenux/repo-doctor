# deps-vulnerable – Vulnerable dependencies

**Category:** Security · **Severity:** HIGH (by severity from OSV) · **Automatic fix:** yes (`uv.lock`, `package-lock.json`)

## What the check looks for

Parses lockfiles (`uv.lock`, `poetry.lock`, `requirements*.txt` with `==`, `pyproject.toml` with a pin, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `go.sum`) and queries **OSV.dev** in batches (`/v1/querybatch`, no key). Severity comes from the database (GHSA: CRITICAL/HIGH → HIGH, MODERATE → MED, LOW → LOW). Skipped in offline mode. Results are cached.

## Automatic fix

In the fix screen (`f`) repo-doctor rewrites a vulnerable dependency in the lockfile to the **lowest fixed version** (per OSV) and stores the result on a new branch. You need neither `uv`, `npm` nor any other program: it takes the version, link and checksum from the PyPI and npm registries itself and edits the lockfile in pure Python. Supported: `uv.lock` and `package-lock.json` (lockfileVersion 3).

The rewrite is deliberately careful and changes only the version of one package. The offer **does not appear** (the manual steps below remain) when the dependency tree would have to be resolved again: the new version has different dependencies, another package forbids it (e.g. `idna<3.5`), the package is not from a public registry, the lockfile has unusual formatting or environment restrictions, or the fixed version changes install scripts. For `uv.lock` the output is also checked against the old version of the same package, so nothing is written that cannot be reproduced exactly. Run the project's tests after merging. `Cargo.lock`, `go.sum`, `yarn.lock`, `pnpm-lock.yaml` and `poetry.lock` are still manual.

## Why it matters

Known vulnerabilities have public exploits; updating is usually the cheapest defence.

## Steps

1. Look up the vulnerability ID (GHSA/CVE/PYSEC) and find the fixed version.
2. Update the dependency. For `uv.lock` and `package-lock.json` the fix screen (`f`) does it; for the others, and where the fix does not help, do it manually (`uv lock --upgrade-package X`, `npm update X`, `cargo update -p X`, `go get X@latest`).
3. If you cannot update, assess the impact and add the finding to the allowlist with a reason.
