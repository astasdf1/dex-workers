---
name: wait
description: Wait for background dex-workers runs and collect their results, or read one finished result by run ID.
argument-hint: "<run-id> [run-id...] [--timeout seconds]"
allowed-tools: Bash
disable-model-invocation: true
---

Run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers wait <run-id>... --timeout <seconds>` with the run IDs returned by `run ... --background`. Each collected result is removed from the state directory unless `--keep` is given. For a single finished run, `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers result <run-id>` returns it without waiting. Summarize each result's `status`, `provider`, `session_id` and `structured` or `output`; a `CLAUDE_FALLBACK` entry means Claude continues that subtask locally.
