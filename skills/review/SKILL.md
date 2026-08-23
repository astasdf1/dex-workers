---
name: review
description: Ask an eligible external worker to review current workspace changes without modifying files.
argument-hint: "[review focus] [provider=auto|codex|agy]"
allowed-tools: Bash
disable-model-invocation: true
---

Run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers review` with a concise review focus and `--cwd` set to the current workspace. Report the structured findings; on `CLAUDE_FALLBACK`, perform the review locally in Claude.
