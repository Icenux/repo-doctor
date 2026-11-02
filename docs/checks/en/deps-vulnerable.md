# deps-vulnerable – Vulnerable dependencies

**Category:** Security · **Severity:** HIGH (by severity from OSV) · **Automatic fix:** yes (`uv.lock`, `package-lock.json`)

## What the check looks for

Parses lockfiles (`uv.lock`, `poetry.lock`, `requirements*.txt` with `==`, `pyproject.toml` with a pin, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `go.sum`) and queries **OSV.dev** in batches (`/v1/querybatch`, no key). Severity comes from the database (GHSA: CRITICAL/HIGH → HIGH, MODERATE → MED, LOW → LOW). Skipped in offline mode. Results are cached.

## Automatic fix

In the fix screen (`f`) repo-doctor updates vulnerable dependencies to the **lowest fixed version** (per OSV) and stores the resulting lockfile on a new branch – no other CLI to run. The package manager (`uv lock --upgrade-package`, `npm update --package-lock-only --ignore-scripts`) runs in a temporary directory containing only the manifest and lockfile from HEAD, so neither the repo nor the working tree is touched and nothing from the repo is executed. It is offered only when `uv` / `npm` is in `PATH` and OSV knows the fixed version. Anything a manifest constraint blocks (e.g. `requests<2.20`) stays as is and the fix says so. `Cargo.lock`, `go.sum`, `yarn.lock` and `pnpm-lock.yaml` are still manual. Run the project's tests after merging.

## Why it matters

Known vulnerabilities have public exploits; updating is usually the cheapest defence.

## Steps

1. Look up the vulnerability ID (GHSA/CVE/PYSEC) and find the fixed version.
2. Update the dependency – in repo-doctor via the fix screen (`f`), elsewhere manually (`uv lock --upgrade-package X`, `npm update X`, `cargo update -p X`, `go get X@latest`).
3. If you cannot update, assess the impact and add the finding to the allowlist with a reason.
