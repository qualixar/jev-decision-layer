# One runtime, five harnesses

The typed questions, local gate, receipts, budgets, and recipe catalog live in one shared runtime at `plugins/qualixar-jev-decision-layer/runtime`. Codex, Claude Code, VS Code, Hermes, and Antigravity connect to it through different adapters. They share decision contracts, but hook timing, configuration, enrollment, and live verification differ by host. A shared runtime is not a claim of equal capabilities or conformance.

The common decision contracts remain separate from each host adapter. This allows shared policy and receipts while preserving the surface and authority rules of each host.

## What each adapter actually is

Harnesses expose different plugin, hook, and tool surfaces. Four adapters include a hook integration; VS Code has no hook surface and uses MCP registration. Every host retains execution authority.

| Harness | Adapter | Surface it attaches to |
|---|---|---|
| Codex | `jev_auto/hooks.py`, `hooks/codex-hooks.json` | Optional `PostToolUse` matcher over documented read-only tools; native hook trust and firing require separate evidence |
| Claude Code | `jev_auto/claude_hook.py`, `hooks/claude-hooks.json` | Claude-specific hook and plugin MCP surfaces. Uses `${CLAUDE_PLUGIN_ROOT}`, not `${PLUGIN_ROOT}`; hook behavior depends on native trust and settings |
| Antigravity | `jev_auto/agy_hook.py`, root `hooks.json` | `PreInvocation` advisory only. Deliberately not `PreToolUse`, whose documented contract requires a permission decision |
| Hermes | `jev_auto/hermes_hook.py`, `jev_auto/hermes_tool.py` | Separate hook and tool entry points |
| VS Code | `jev_auto/vscode_adapter.py` | No hook surface at all. Registers the stdio launcher as a workspace MCP server in `.vscode/mcp.json` |

The adapters offer host-native tool registration where available. VS Code uses workspace `.vscode/mcp.json`; Antigravity uses its documented global `mcpServers` configuration; Claude desktop app configuration is distinct from Claude Code CLI. Host registration describes configuration shape; it does not widen the supported operating-system scope stated below.

**Hook files are per host and are never merged.** Each harness reads its own file and they have incompatible schemas — Codex's `hooks/codex-hooks.json` and Claude Code's `hooks/claude-hooks.json` are different documents on purpose. Editing one to satisfy another breaks the first silently.

**`.vscode/mcp.json` keys servers under `servers`, not `mcpServers`.** VS Code ignores the wrong key without an error, producing no server and no diagnostic. The adapter writes the documented shape and a test asserts it, because this is not a mistake review catches.

## Platform scope

Version 1.0.10 is supported and verified on macOS, using Keychain for hosted TypeSafe Jev and OpenRouter. Linux remains experimental and unverified: generic Linux CI does not establish the complete Secret Service, broker, host, and provider path. Windows hosted runtime entry points fail closed because the native private-state contract did not pass CI; the Windows CI lane has been removed until that contract is deliberately revalidated. An adapter being present does not prove the full host + operating system + provider path works.

## Shared enrollment

Codex, Claude Code, Hermes, Antigravity, and VS Code resolve workspace consent through one binding. An exact policy wins. A root grant covers child directories and nested repositories only after a separate approval in the setup wizard or `jev enroll --cover-descendants`. A child policy that you revoked, or a local refusal, stays in force and is not reopened by the root. A child grant that simply expired no longer switches the folder off: it falls through to an approved root. Home directories and filesystem tops cannot be descendant roots. The broker and budget stay on the grant root.

`native_status` remains `NOT_RUN` for every host. The shared binding is covered by in-process adapter tests. That is not a native host session, and it does not change the macOS, Linux, or Windows platform scope above.


Windows-specific filesystem, credential, pipe, and launcher code remains in the source tree for future work, but it is not a supported 1.0.10 runtime. The `jev_auto` broker refuses to start on Windows. Windows host configuration snippets do not enable or imply runtime support.

Local Laya-MLX remains limited to a compatible Apple-Silicon Mac with a successful local installation attestation. It is not a Windows or Linux provider route. Hosted and local routes are separate choices; Jev-only does not silently fall back to Laya.

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

**Hooks.** The plugin registers `SessionStart`, `UserPromptSubmit` and `SubagentStart`; none of them can grant or deny a tool call, and `PreToolUse` is not registered. In an enrolled workspace the guidance names the exact `workspace_path`, `data_classification` and `provider` the grant accepts, and lists the typed tools only when the grant enables generic typed queries. `SubagentStart` returns the same guidance as `hookSpecificOutput.additionalContext`, because a subagent does not inherit SessionStart context. A folder with no active grant, including one whose grant expired, gets one line at session start that says so and tells the model not to enroll it. A folder you revoked or refused stays silent.

**Missing `workspace_path`.** When Claude Code spawns the server with `CLAUDE_PROJECT_DIR` set and a tool call omits `workspace_path`, the server uses that directory. Every advertised schema still requires the argument, and hosts that do not set the variable are unaffected. The server cannot tell which host exported the variable: if you export `CLAUDE_PROJECT_DIR` globally, any host that passes its environment to the server gets the same fallback. Consent is still checked on the resolved folder.

**Where the MCP server loads.** Plugin-provided MCP servers are read by the Claude Code CLI and by on-machine Cowork sessions. They are **not** loaded by the desktop app's Code tab. This is a property of that surface, not of this plugin: in a Code tab session, every enabled plugin that ships an MCP server is equally absent, with no error and no failed entry. Commands and skills load normally there. To get the tools in the Code tab, register the launcher directly:

```sh
claude mcp add qualixar-jev -- "$HOME/.claude/plugins/cache/qualixar/qualixar-jev-decision-layer/1.0.11/scripts/launch-jev"
```

For the desktop app specifically, add the same command to `~/Library/Application Support/Claude/claude_desktop_config.json` and restart it. Note that `claude mcp list` reports on the CLI's own configuration and says nothing about what the desktop app can see — a green line there is not evidence the app loaded anything.

### VS Code

```sh
plugins/qualixar-jev-decision-layer/scripts/jev vscode --workspace .
```

This writes nothing. It prints the planned change, the config path, and `preserved_servers` — your existing servers, which are kept. On supported macOS, add `--write` to apply. Linux registration code is experimental and unverified; Windows hosted runtime is disabled in 1.0.10. An existing `.vscode/mcp.json` is merged: exactly one `qualixar-jev` entry is added or updated and every other key is carried through. A file that does not parse is refused rather than overwritten, because rewriting it would discard servers the adapter cannot read. Restart VS Code afterwards; Copilot agent mode reads the workspace file.

### Antigravity

Use the portable plugin source at `plugins/qualixar-jev-decision-layer` with Antigravity's own plugin install path. It picks up the root `hooks.json` PreInvocation advisory. For the decision tools, register the MCP server in Antigravity's global config:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev host-register --host antigravity
```

On supported macOS, add `--write` to apply. Linux registration code is experimental and unverified. Windows is outside the supported 1.0.10 runtime. A plugin-relative `mcp_config.json` is deliberately **not** shipped: Antigravity documents `command` as an executable or a binary name and says nothing about resolving a path relative to the plugin, so the adapter uses an absolute one in the documented global config. A repository test keeps that file from being added until the relative form is documented and verified.

### Hermes

Use the portable plugin source with Hermes's own plugin install path; it uses separate hook and tool entry points and an explicit tool allow-list. The 1.0.10 source includes offline self-test, `jev_verify`, and `jev_rerank` in that allow-list. A host manifest or tool-list handshake does not prove a native model/tool turn.

### Claude desktop app

```sh
plugins/qualixar-jev-decision-layer/scripts/jev host-register --host claude-desktop --write
```

**Quit the app first.** It holds its config in memory and flushes it on exit, so an edit made while it is running is silently discarded — measured, not assumed. Re-run the command after every upgrade: the plugin cache path carries the release number. An entry that points at an older release of the same launcher in the same cache is replaced; any other existing `qualixar-jev` entry, including one with your own `env` or `args`, or a newer release, is refused as `HOST_MCP_ENTRY_CONFLICT`. This applies to every host `host-register` supports. Windows runtime commands are disabled in 1.0.10; do not use a generated configuration snippet as an indication of platform support.

## What is verified, and what is not

"Evidence" means something was run and produced the stated result. "Boundary" is what that evidence does *not* establish. No row here claims measured token, cost or time savings, because none has been measured.

| Harness | Evidence | Boundary |
|---|---|---|
| **Codex** | Installed MCP tools and live synthetic TypeSafe routing verified on macOS. The Codex hook uses the shared enrollment binding, including an approved root grant over a nested repository, in process | Does not establish a native Codex hook firing, Linux operation, or Windows runtime support. `native_status` remains `NOT_RUN` |
| **Claude Code** | Plugin installs from the repo marketplace and `claude plugin validate` passes; seven commands and three skills load; the MCP launcher answers an `initialize` handshake; the hook launcher exits cleanly and follows the shared enrollment binding for an approved root; in process, the hook emits argument-bearing guidance for each consent state, including `SubagentStart` | Hook output is pinned by in-process tests, not by a live session's model context. A native MCP tool turn inside a live session, and hook firing under a host that permits plugin hooks, are not yet verified. In the desktop app's Code tab the plugin-provided server does not load at all. `native_status` remains `NOT_RUN` |
| **Hermes** | Staged 1.0.10 source registers twelve declared tools including self-test, verify and rerank, plus its hook. The hook follows the shared enrollment binding | Native model/tool turn still needs verification. `native_status` remains `NOT_RUN` |
| **Antigravity** | Packaged PreInvocation advisory hook and skills; this adapter does not request PreToolUse authority. The hook follows the shared enrollment binding | Native model/tool turn and portable MCP registration still need verification. `native_status` remains `NOT_RUN` |
| **VS Code** | Adapter writes a valid `.vscode/mcp.json` against the documented `servers` format; merge, refusal and symlink behaviour covered by tests. The registered server uses the shared enrollment binding | No live Copilot agent-mode turn has been run. No extension ships; registration is the whole integration. `native_status` remains `NOT_RUN` |

`native_adapter` in the host inventory means this package ships a shim for that harness. It is derived from files in this repository and never from probing a host, and it is not a conformance claim — `native_status` stays `NOT_RUN` for every row until someone runs one.

## Check it before you spend anything

Every recipe ships three synthetic cases. Replaying all 114 through the real gate costs no provider call, no key, and no workspace enrolment:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest
```

Or `jev_recipe_selftest` from any host with the MCP tools. A passing run means the shipped gate still matches its recorded contract. It is a contract check and never evidence of the provider's accuracy.

## Adding a sixth harness

1. Decide what the harness exposes: a hook, a tool registration, or only an MCP config file.
2. Write a shim under `runtime/jev_auto/` that adapts that surface to the existing runtime. Do not duplicate decision logic, and do not vendor a second runtime.
3. Give it its own hook or config file. Never extend another host's.
4. Add the host to `_HOSTS` in `runtime/src/adl/api/host_inventory.py`, and to `_NATIVE_ADAPTERS` only once a shim actually ships.
5. Add its hash to `runtime/RUNTIME_MANIFEST.json` and `git add` the file — the manifest test scans the package files on disk, including untracked source files, while excluding Python caches.
6. Add the row to the tables above with honest evidence and boundary columns.
