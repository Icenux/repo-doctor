# secrets-history – Secrets in commit history

**Category:** Security · **Severity:** HIGH · **Automatic fix:** no

## What the check looks for

Goes through the whole history (`git log --all -p`) and scans added lines with the same rules as `secrets-tree`. The finding shows the commit that added the secret. On huge repositories the scan stops after the time limit (`limits.history_timeout_s`) with a `scan-incomplete` finding. Obviously made-up values are hidden the same way as in `secrets-tree` (option `hide_fake_secrets`).

## Why it matters

The key stays in history even after the file is deleted. Anyone who clones the repo gets the key. It cannot be fixed automatically – rewriting history is always your decision.

## Steps

1. Revoke the key right away and generate a new one (always rotate).
2. Remove it from history: `git filter-repo --path config/prod.env --invert-paths` (or `--replace-text`).
3. Force push and let any collaborators know (they must clone again).
4. Add the file to `.gitignore` (the `gitignore-incomplete` / `env-committed` fix prepares that).

## Note

Cannot be fixed automatically: rewriting history is always your decision.
