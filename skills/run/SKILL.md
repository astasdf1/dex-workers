---
name: run
description: Delegate a bounded read-only task to an eligible Codex or Antigravity worker, with optional model, effort, session resume, structured output, images, brief, and background execution.
argument-hint: "<task> [provider=auto|codex|agy] [model=...] [effort=low|medium|high|xhigh] [resume=<session>] [background]"
allowed-tools: Bash
disable-model-invocation: true
---

Run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers run` with the user's task as one quoted argument and the current workspace as `--cwd`. It is read-only by default. Add `--write` only when the user explicitly authorizes workspace changes.

Map the user's words onto flags:
- `provider=` → `--provider codex|agy` (otherwise automatic routing).
- `model=` → `--model <name>`; a suffix such as `gpt-5.5-high` or `gpt-5.5:high` pins the effort.
- `effort=` → `--effort minimal|low|medium|high|xhigh|max`; values above what the provider offers are clamped. `--role review|audit|implementation` sets a default effort (low, high, none).
- `resume=` → `--resume <session_id>` continues the Codex thread or agy conversation from an earlier result's `session_id`.
- a request for structured output → `--findings` (bundled findings schema) or `--schema <file>`; the parsed document is returned as `structured`.
- screenshots or images → `--image <file>` per file.
- a longer or careful handoff → `--brief` (adds the standard template with git state), plus `--deliverable`, `--done-when` and `--context <file>` as given.
- `background` → `--background`; then collect with `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers wait <run_id>`.

Return the structured result. Quote `session_id` and `resume_hint` so the user can continue the same worker. If status is `CLAUDE_FALLBACK`, continue the task yourself in Claude.
