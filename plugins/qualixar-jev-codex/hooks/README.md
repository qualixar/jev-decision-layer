# Per-host files

Some files in this package belong to exactly one host. They look
interchangeable and they are not: each host expands its own plugin-root
variable and ignores the others, so the wrong file produces a command that
points nowhere. Nothing errors. The host simply has no hooks, or no tools.

## Hook files, in this directory

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

The Codex package does not ship `claude-hooks.json` at all: it is another
host's file, and `tools/build_codex_package.py` excludes it by name.

## MCP connection descriptors, one level up

The same rule, and the reason this section exists: for four releases it was
written down only for hooks, so the descriptors repeated the mistake.

| File | Host | Command |
|---|---|---|
| `../.mcp.json` | Claude Code | `${CLAUDE_PLUGIN_ROOT}/scripts/launch-jev` |
| `../mcp.json` | Codex | `./scripts/launch-jev` with `cwd: "."` |

Both start the same bundled server. `1.0.1` copied the Claude descriptor over
the Codex one, so Codex was told to run `${CLAUDE_PLUGIN_ROOT}/scripts/launch-jev`
— a path that does not exist for it — and could not start its server through
1.0.5. A package test asserted the two files were byte-identical, so the test
suite enforced the defect rather than catching it.

The Codex package uses `mcp.json`, the same name as here, because its
`.codex-plugin/plugin.json` is copied verbatim from this package: two names
would make the copied manifest point at a file that is not there.

Hosts that take an absolute path instead — VS Code, Antigravity, the Claude
desktop app — are registered by `jev_auto/host_mcp.py`, which resolves the
launcher absolutely and never emits a variable at all.

## The guard

`tests/test_host_surface_parity.py` reads each manifest, follows what it points
at, and fails if a host is handed a variable it does not expand or if a
host-facing file ships that no manifest names. It also runs the Codex package
and release-version checks, so a derived package cannot drift out of sync while
the suite still passes. Adding a host means adding a row there. This README
explains why; the test is what actually holds.
