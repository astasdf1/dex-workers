---
name: setup
description: Install or update dex-workers' user-level default delegation protocol without overwriting unrelated Claude instructions.
allowed-tools: Bash
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/setup.py setup-user --dry-run`, show the planned managed-section change, and only apply it when the user requested setup. Then run the same command without `--dry-run`. Existing content is preserved, a timestamped backup is created, and malformed managed markers must be reported rather than repaired silently.

Plugin installation alone exposes this capability; it does not silently mutate `~/.claude/CLAUDE.md`.
