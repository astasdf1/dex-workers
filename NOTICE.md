# Provenance and update ownership

- Owner and updater: DEX-2 maintainers.
- The plugin is a standalone Python-standard-library payload. It imports no code from `dex-usage` and has no Node, npm, FlowDesk, or MCP runtime dependency.
- Codex CLI and Antigravity `agy` are external, locally installed tools. Their executable paths are discovered at runtime; absence, failed login evidence, and unsupported capabilities fail closed to `CLAUDE_FALLBACK`.
- Claude Code plugin metadata, `${CLAUDE_PLUGIN_ROOT}`, skills, and marketplace layout follow Claude Code plugin conventions.
