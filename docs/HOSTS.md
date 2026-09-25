# One runtime, five harnesses

This layer is host-neutral by construction. The decision logic — typed questions, the local gate, receipts, budgets, the recipe catalog — lives in a single runtime at `plugins/qualixar-jev-decision-layer/runtime`. A host adapter is a thin shim that carries that runtime into whatever surface a particular harness exposes, and nothing more. Adding a harness means writing a shim; it never means forking the decision logic.

That is the whole design claim, and it is worth stating plainly because the alternative is common: a separate integration per harness, each drifting until the same question gets different answers in different tools.

## What each adapter actually is

Harnesses do not agree on how a plugin attaches. Four expose a hook or tool surface; one does not. The adapter is chosen by what the host offers, not by preference.

| Harness | Adapter | Surface it attaches to |
|---|---|---|
| Codex | `jev_auto/hooks.py`, `hooks/hooks.json` | A `PostToolUse` matcher over documented read-only tools |
| Claude Code | `jev_auto/claude_hook.py`, `hooks/claude-hooks.json` | `PreToolUse` can be advisory-only here, so a hint costs no authority. Uses `${CLAUDE_PLUGIN_ROOT}`, not `${PLUGIN_ROOT}` |
| Antigravity | `jev_auto/agy_hook.py`, root `hooks.json` | `PreInvocation` advisory. Deliberately **not** `PreToolUse`: that contract requires a permission `decision` and would widen host trust |
| Hermes | `jev_auto/hermes_hook.py`, `jev_auto/hermes_tool.py` | Separate hook and tool entry points |
| VS Code | `jev_auto/vscode_adapter.py` | No hook surface at all. Registers the stdio launcher as a workspace MCP server in `.vscode/mcp.json` |

**Hook files are per host and are never merged.** Each harness reads its own file and they have incompatible schemas — Codex's `hooks/hooks.json` and Claude Code's `hooks/claude-hooks.json` are different documents on purpose. Editing one to satisfy another breaks the first silently.

**`.vscode/mcp.json` keys servers under `servers`, not `mcpServers`.** VS Code ignores the wrong key without an error, producing no server and no diagnostic. The adapter writes the documented shape and a test asserts it, because this is not a mistake review catches.

## Install

Every host runs the same runtime from the same portable source at `plugins/qualixar-jev-decision-layer`.

### Codex Desktop

Install from this repository's **Qualixar Jev Layer** marketplace, then start a new task.

```sh
codex plugin marketplace add qualixar/jev-decision-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

### Claude Code

```sh
claude plugin marketplace add qualixar/jev-decision-layer
claude plugin install qualixar-jev-decision-layer@qualixar
```

Adds seven commands — `/jev-setup`, `/jev-status`, `/jev-route`, `/jev-recipes`, `/jev-review`, `/jev-selftest`, `/jev-vscode` — plus the shared skills and the `qualixar-jev` MCP server.

**Where the MCP server loads.** Plugin-provided MCP servers are read by the Claude Code CLI and by on-machine Cowork sessions. They are **not** loaded by the desktop app's Code tab. This is a property of that surface, not of this plugin: in a Code tab session, every enabled plugin that ships an MCP server is equally absent, with no error and no failed entry. Commands and skills load normally there. To get the tools in the Code tab, register the launcher directly:

```sh
claude mcp add qualixar-jev -- "$HOME/.claude/plugins/cache/qualixar/qualixar-jev-decision-layer/1.0.1/scripts/launch-jev"
```

For the desktop app specifically, add the same command to `~/Library/Application Support/Claude/claude_desktop_config.json` and restart it. Note that `claude mcp list` reports on the CLI's own configuration and says nothing about what the desktop app can see — a green line there is not evidence the app loaded anything.

### VS Code

```sh
python3 -m jev_auto.cli vscode --workspace .
```

This writes nothing. It prints the planned change, the config path, and `preserved_servers` — your existing servers, which are kept. Add `--write` to apply. An existing `.vscode/mcp.json` is merged: exactly one `qualixar-jev` entry is added or updated and every other key is carried through. A file that does not parse is refused rather than overwritten, because rewriting it would discard servers the adapter cannot read. Restart VS Code afterwards; Copilot agent mode reads the workspace file.

### Antigravity and Hermes

Use the portable plugin source at `plugins/qualixar-jev-decision-layer` with the host's own plugin install path. Antigravity picks up the root `hooks.json` PreInvocation advisory; Hermes uses its own hook and tool entry points.

## What is verified, and what is not

"Evidence" means something was run and produced the stated result. "Boundary" is what that evidence does *not* establish. No row here claims measured token, cost or time savings, because none has been measured.

| Harness | Evidence | Boundary |
|---|---|---|
| **Codex Desktop** | Installed MCP tools and live synthetic TypeSafe routing verified on a Mac | Automatic-hook coverage and savings are not proved by that call |
| **Claude Code** | Plugin installs from the repo marketplace and `claude plugin validate` passes; seven commands and three skills load; the MCP launcher answers an `initialize` handshake; the hook launcher exits cleanly and stays silent on an unenrolled workspace | A native MCP tool turn inside a live session, and hook firing under a host that permits plugin hooks, are not yet verified. In the desktop app's Code tab the plugin-provided server does not load at all |
| **Hermes** | Staged plugin doctor registers 9 tools and 1 hook; installed copy awaits refresh | Native model/tool turn still needs verification |
| **Antigravity** | Packaged PreInvocation advisory hook and 2 skills; host adapter previously validated | No portable Jev MCP launcher or native model/tool turn verified |
| **VS Code** | Adapter writes a valid `.vscode/mcp.json` against the documented `servers` format; merge, refusal and symlink behaviour covered by tests | No live Copilot agent-mode turn has been run. No extension ships; registration is the whole integration |

`native_adapter` in the host inventory means this package ships a shim for that harness. It is derived from files in this repository and never from probing a host, and it is not a conformance claim — `native_status` stays `NOT_RUN` for every row until someone runs one.

## Check it before you spend anything

Every recipe ships three synthetic cases. Replaying all 96 through the real gate costs no provider call, no key, and no workspace enrolment:

```sh
python3 -m jev_auto.cli selftest
```

Or `jev_recipe_selftest` from any host with the MCP tools. A passing run means the shipped gate still matches its recorded contract. It is a contract check and never evidence of the provider's accuracy.

## Adding a sixth harness

1. Decide what the harness exposes: a hook, a tool registration, or only an MCP config file.
2. Write a shim under `runtime/jev_auto/` that adapts that surface to the existing runtime. Do not duplicate decision logic, and do not vendor a second runtime.
3. Give it its own hook or config file. Never extend another host's.
4. Add the host to `_HOSTS` in `runtime/src/adl/api/host_inventory.py`, and to `_NATIVE_ADAPTERS` only once a shim actually ships.
5. Add its hash to `runtime/RUNTIME_MANIFEST.json` and `git add` the file — the manifest test compares against `git ls-files`.
6. Add the row to the tables above with honest evidence and boundary columns.
