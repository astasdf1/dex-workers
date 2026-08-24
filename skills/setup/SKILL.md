---
name: setup
description: Inspect or explicitly manage dex-workers' automatically installed user-level delegation policy.
allowed-tools: Bash
---

The async SessionStart hook normally applies this policy automatically. For explicit setup, run `${CLAUDE_PLUGIN_ROOT}/scripts/setup.py setup-user --dry-run`, show the planned managed-section change, then apply it with `setup-user`. Existing content is preserved, a timestamped backup is created, and malformed or duplicate managed markers must be reported rather than repaired silently.

For disable, run `disable-auto-policy`; its durable state prevents future hooks from re-enabling the policy. For restore/removal, run `restore-user`, which backs up the file, removes only a valid managed block, and records the opt-out. Only run `enable-auto-policy` when the user explicitly asks to opt back in.
