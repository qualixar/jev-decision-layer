# Hook files in this directory

Each host gets its OWN file. `hooks.json` is NOT shared.

| File | Host | Plugin-root variable |
|---|---|---|
| `hooks.json` | Codex | `${PLUGIN_ROOT}` |
| `claude-hooks.json` | Claude Code | `${CLAUDE_PLUGIN_ROOT}` |

Antigravity's `PreInvocation` hook is declared separately in the plugin-root
`hooks.json` (one level up), not here.

**Do not merge these.** The variable names differ per host, and Codex
registers a `PostToolUse` matcher that Claude Code deliberately does not:
`tests/test_codex_hook_coverage.py` asserts that matcher exists and covers
only documented read-only tools.

Claude Code finds `hooks.json` by default, so the Claude file MUST stay
declared explicitly via `"hooks": "./hooks/claude-hooks.json"` in
`.claude-plugin/plugin.json` — otherwise Claude Code would load Codex's
hooks, whose `${PLUGIN_ROOT}` it cannot resolve.
