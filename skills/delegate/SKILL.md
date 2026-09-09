---
name: delegate
description: Automatically route a bounded subtask delegated by the main Claude agent to Claude native subagents, Codex, or Antigravity according to readiness and dex-usage quota. Use when Claude decides to delegate part of a larger task; do not use for the whole user request or ordinary work Claude can perform directly.
allowed-tools: Bash, Task
---

Claude remains the main agent. Invoke this skill only after the main Claude has decided that a distinct, bounded subtask should be delegated. Never intercept or rewrite the user's prompt, and never ask the user to run a command.

1. Classify and restate one bounded subtask with a concrete deliverable, completion criteria, relevant workspace scope, and read-only versus explicitly user-authorized write access. Do not delegate the entire request.
2. Classify the role: `review` for ordinary diff/regression/UI review, `audit` for thorough/deep/high-risk/accuracy-critical verification, and `implementation` for build/fix/change work. Run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers select --role <role> --mode single --task "<bounded subtask>"` exactly once. Antigravity is preferred for ordinary reviews, is not routinely selected for implementation, and is only supplemental for audits. Do not request separate approval before launching authenticated `agy`.
3. Read the structured `selection` value:
   - For `codex` or `agy`, run `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers run "<bounded subtask>" --provider <selection> --cwd "<workspace>" --role <role> --brief --deliverable "<deliverable>" --done-when "<criteria>"`. Runs are read-only by default. Add `--write` only when the user explicitly authorized that subtask to modify the workspace. Add `--findings` for review and audit so the result carries `structured.findings`. Add `--context <file>` for small files the worker must see, `--image <file>` for screenshots, and `--model`/`--effort` only when the user named them; the role already sets the default effort.
   - For `CLAUDE_NATIVE`, use Claude Code's `Task` tool to create a native subagent for the bounded subtask. Do not run the external-worker executable. The plugin also ships `codex-reviewer`, `codex-implementer` and `codex-auditor` agents for callers that prefer `Task` with an explicit Codex route.
4. When several independent subtasks are delegated at once, start each external run with `--background` (at most 5 active), run any native Task meanwhile, and collect with `${CLAUDE_PLUGIN_ROOT}/bin/dex-workers wait <run_id>...`. Only one writer per worktree.
5. If an external run returns `CLAUDE_FALLBACK`, immediately perform the same bounded subtask with a Claude native `Task` subagent.
6. To follow up with the same worker (a clarification, a re-check, the next step of its task), run again with `--resume <session_id>` from its result instead of a fresh prompt, so its context is preserved.
7. Return the delegated result to the main Claude agent, including the selected route, model and effort, `session_id`, useful output or `structured` findings, verification evidence, and any blocker. The main Claude owns integration and the final user response.

The `run`, `review`, `wait`, `status`, `doctor`, and `cancel` skills are optional diagnostics and manual controls; they are not prerequisites for automatic delegation.
