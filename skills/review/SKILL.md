---
name: review
description: Review changes in ordinary single-review or multi-perspective mode using Claude, Codex, and Antigravity according to readiness and quota.
argument-hint: "[review focus] [mode=single|multi] [role=review|audit]"
allowed-tools: Bash, Task
---

Classify ordinary review as `review`; classify thorough, deep, high-risk, or accuracy-critical verification as `audit`.

For a single review, call `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers select --role <role> --mode single --task "<focus>"`. Ordinary review prefers Antigravity. Audit selects Claude native or Codex as the primary verifier. Execute the selected route read-only; external failure falls back to a Claude native Task.

For multi-perspective review, call the selector with `--mode multi`. Independently dispatch every provider in `selections`: Claude via Task, Codex via `dex-workers review ... --provider codex`, and Antigravity via `dex-workers review ... --provider agy`. Launch independent routes in parallel when supported. A provider is excluded only when unavailable or its reliable known remaining quota is below 5%; unknown quota remains eligible. Continue after partial failure.
If `selections` is empty, report that no reviewer is eligible and do not bypass the quota threshold.

The main Claude synthesizes all results, deduplicates findings, compares conflicts, and preserves provider attribution. For audit/multi review, final verdicts and critical findings must be anchored or confirmed by Claude or Codex. Antigravity-only findings are labeled supplemental/unconfirmed and are never decisive.
