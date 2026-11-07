# secrets-tree – Secrets in current files

**Category:** Security · **Severity:** HIGH · **Automatic fix:** no

## What the check looks for

Goes through tracked and untracked (not ignored) files and looks for API keys (AWS, GitHub, GitLab, Slack, OpenAI, Anthropic), private SSH/PGP keys, JWTs, connection strings with a password and generic high-entropy secrets. If `gitleaks` is installed, its detection is used. Lockfiles and test fixtures (`fixtures/`, `testdata/`, `__snapshots__/`) are skipped, as are lines marked `gitleaks:allow` or `repo-doctor:allow`. Obviously made-up values (`abcdef…`, `123456…`, `9f8e7d6c5b4a`, truncated `abc...xyz`) are not reported, including those found by `gitleaks`; the report shows how many were hidden (`Hidden as obviously made-up values`) and the filter can be turned off with `hide_fake_secrets = false` in `config.toml`. A real weak password such as `Company123456` is still reported.

## Why it matters

A secret in a versioned file gets into every clone, backup and fork. Once committed, it stays in history even after deletion.

## Steps

1. Revoke the key right away and generate a new one – rotation is always necessary, even if the repo is not public.
2. Move the value to an environment variable, a password manager (`pass`) or a file outside the repo.
3. Add the file to `.gitignore`; if it is already committed, also deal with the `secrets-history` check.
4. If it is a false alarm, add the finding to the allowlist (`a` on the repo card) with a reason.
