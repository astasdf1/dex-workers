---
name: run
description: Delegate a bounded read-only task to an eligible Codex or Antigravity worker.
argument-hint: "<task> [provider=auto|codex|agy]"
allowed-tools: Bash
disable-model-invocation: true
---

Run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers run` with the user's task as one quoted argument. Use `--provider` only if requested and pass the current workspace with `--cwd`. It is read-only by default. Add `--write` only when the user explicitly authorizes workspace changes. Return the structured result. If status is `CLAUDE_FALLBACK`, continue the task yourself in Claude.
