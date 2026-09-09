---
name: review
description: Review changes in ordinary single-review or multi-perspective mode using Claude, Codex, and Antigravity according to readiness and quota.
argument-hint: "[review focus] [mode=single|multi] [role=review|audit]"
allowed-tools: Bash, Task
---

Classify ordinary review as `review`; classify thorough, deep, high-risk, or accuracy-critical verification as `audit`.

For a single review, call `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers select --role <role> --mode single --task "<focus>"`. Ordinary review prefers Antigravity. Audit selects Claude native or Codex as the primary verifier. Execute the selected route read-only with `dex-workers review "<focus>" --cwd "<workspace>" --provider <selection> --findings --brief --effort <suggested_effort>`; the `select` result carries `suggested_effort`. External failure falls back to a Claude native Task.

For multi-perspective review, call the selector with `--mode multi`. Independently dispatch every provider in `selections`: Claude via Task, Codex via `dex-workers review ... --provider codex --findings --background`, and Antigravity via `dex-workers review ... --provider agy --findings --background`. Launch independent routes in parallel: start the external ones with `--background`, run the Claude Task, then collect the external results with `dex-workers wait <run_id> <run_id>`. A provider is excluded only when unavailable or its reliable known remaining quota is below 5%; unknown quota remains eligible. Continue after partial failure.
If `selections` is empty, report that no reviewer is eligible and do not bypass the quota threshold.

The main Claude synthesizes all results, deduplicates findings, compares conflicts, and preserves provider attribution. Use each result's `structured.findings` (file, line, severity, summary, failure_scenario, confidence) when present so findings from different providers can be matched by file and line. For audit/multi review, final verdicts and critical findings must be anchored or confirmed by Claude or Codex. Antigravity-only findings are labeled supplemental/unconfirmed and are never decisive. To ask a provider to re-check one of its own findings, run again with `--resume <session_id>` rather than a fresh prompt.
