---
name: cancel
description: Cancel a currently active subprocess managed by dex-workers using its run ID.
argument-hint: "<run-id>"
allowed-tools: Bash
disable-model-invocation: true
---

Run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers cancel` with the exact run ID from a prior structured result or status output. Do not kill arbitrary PIDs.
