---
name: setup
description: Inspect or explicitly manage dex-workers' automatically installed user-level delegation policy.
allowed-tools: Bash
---

The async SessionStart hook normally applies this policy automatically. For explicit setup, run `${CLAUDE_PLUGIN_ROOT}/scripts/setup.py setup-user --dry-run`, show the planned managed-section change, then apply it with `setup-user`. Existing content is preserved, a timestamped backup is created, and malformed or duplicate managed markers must be reported rather than repaired silently.

For disable, run `disable-auto-policy`; its durable state prevents future hooks from re-enabling the policy. For restore/removal, run `restore-user`, which backs up the file, removes only a valid managed block, and records the opt-out. Only run `enable-auto-policy` when the user explicitly asks to opt back in.

For Antigravity, run `${CLAUDE_PLUGIN_ROOT}/scripts/setup.py setup-agy --dry-run` and then `setup-agy` to add the harness's read-only inspection commands (`git`, `diff`, `grep`, `sed`, `awk`, ...) to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`; headless `agy` auto-denies anything not listed and returns an empty review. Existing rules and other keys are preserved and the file is backed up first. Add `--with-verify` only when the user wants agy to run test runners (`python3`, `pytest`, `npm`, `go`, ...), and `--prune-trusted` to drop `trustedWorkspaces` entries whose directory no longer exists. `dex-workers doctor` reports `harness_permissions` for agy. If a run's `stderr` says a tool required the `unsandboxed` permission, agy's sandbox could not mount a trusted workspace (typically a cloud-synced path); offer `setup-agy --unsandboxed`, which allows only the pure inspection commands outside the sandbox, and explain that plan mode does not stop shell writes. Headless agy also needs `read_file(<dir>/**)` for the workspace root it reviews; pass `--workspace <repo root>` (repeatable) for each repository agy will review; a glob on a parent directory is not honoured by agy.
