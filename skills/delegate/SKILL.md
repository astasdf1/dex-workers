---
name: delegate
description: Automatically route a bounded subtask delegated by the main Claude agent to Claude native subagents, Codex, or Antigravity according to readiness and dex-usage quota. Use when Claude decides to delegate part of a larger task; do not use for the whole user request or ordinary work Claude can perform directly.
allowed-tools: Bash, Task
---

Claude remains the main agent. Invoke this skill only after the main Claude has decided that a distinct, bounded subtask should be delegated. Never intercept or rewrite the user's prompt, and never ask the user to run a command.

1. Classify and restate one bounded subtask with a concrete deliverable, relevant workspace scope, and read-only versus explicitly user-authorized write access. Do not delegate the entire request.
2. Classify the role: `review` for ordinary diff/regression/UI review, `audit` for thorough/deep/high-risk/accuracy-critical verification, and `implementation` for build/fix/change work. Run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers select --role <role> --mode single --task "<bounded subtask>"` exactly once. Antigravity is preferred for ordinary reviews, is not routinely selected for implementation, and is only supplemental for audits. Do not request separate approval before launching authenticated `agy`.
3. Read the structured `selection` value:
   - For `codex` or `agy`, run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers run "<bounded subtask>" --provider <selection> --cwd "<workspace>"`. Runs are read-only by default. Add `--write` only when the user explicitly authorized that subtask to modify the workspace.
   - For `CLAUDE_NATIVE`, use Claude Code's `Task` tool to create a native subagent for the bounded subtask. Do not run the external-worker executable.
4. If an external run returns `CLAUDE_FALLBACK`, immediately perform the same bounded subtask with a Claude native `Task` subagent.
5. Return the delegated result to the main Claude agent, including the selected route, useful output, verification evidence, and any blocker. The main Claude owns integration and the final user response.

The `run`, `review`, `status`, `doctor`, and `cancel` skills are optional diagnostics and manual controls; they are not prerequisites for automatic delegation.
