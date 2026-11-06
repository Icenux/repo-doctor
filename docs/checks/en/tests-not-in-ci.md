# tests-not-in-ci – Tests exist but CI does not run them

**Category:** Maintenance · **Severity:** LOW · **Automatic fix:** no

## What the check looks for

Applies when the repository has both tests and a CI configuration (`.github/workflows`, `.forgejo/workflows`, `.gitea/workflows`, `.gitlab-ci.yml`, `.woodpecker*`, `.circleci`, `Jenkinsfile`, `.travis.yml`...) but none of the CI files contains a command that runs the tests (`pytest`, `tox`, `npm test`, `cargo test`, `go test`, `make test`, `gradle`, `mvn` and the like). CI files composed of others (`uses: ./...`, `include:`, `extends:`) or unreadable ones are not guessed at.

## Why it matters

Tests that CI does not run eventually stop passing and nobody notices. A green badge then merely claims everything is fine.

## Steps

1. Add a test step to the CI configuration (e.g. `pytest`, `npm test`).
2. If you use a custom script or tool this check does not recognize, add the finding to the allowlist.
3. Verify that CI really runs and is green (the `forge-ci-failing` check).
