---
name: setup-project
description: Add an explicit, portable minimal .harness workspace to the current project without overwriting existing files.
allowed-tools: Bash
---

When the user asks to initialize the project harness, first run `${CLAUDE_PLUGIN_ROOT}/scripts/setup.py setup-project --target "<project>" --dry-run`. Report conflicts and stop if any existing harness file differs. Otherwise apply without `--dry-run`, then run `--check` and `./.harness/verify`.

Never modify project `CLAUDE.md` or `AGENTS.md`. Plugin installation only enables this command; project initialization is explicit.
