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

Release 1.0.13 is supported and verified on macOS, using Keychain for hosted TypeSafe Jev and OpenRouter. Linux remains experimental and unverified: generic Linux CI does not establish the complete Secret Service, broker, host, and provider path. Windows hosted runtime entry points fail closed because the native private-state contract did not pass CI; the Windows CI lane has been removed until that contract is deliberately revalidated. An adapter being present does not prove the full host + operating system + provider path works.

## Shared enrollment

Codex, Claude Code, Hermes, Antigravity, and VS Code resolve workspace consent through one binding. An exact policy wins. A root grant covers child directories and nested repositories only after a separate approval in the setup wizard or `jev enroll --cover-descendants`. A child policy that you revoked, or a local refusal, stays in force and is not reopened by the root. A child grant that simply expired no longer switches the folder off: it falls through to an approved root. Home directories and filesystem tops cannot be descendant roots. The broker and budget stay on the grant root.

`native_status` remains `NOT_RUN` for every host. The shared binding is covered by in-process adapter tests. That is not a native host session, and it does not change the macOS, Linux, or Windows platform scope above.


Windows-specific filesystem, credential, pipe, and launcher code remains in the source tree for future work, but it is not a supported runtime in this release. The `jev_auto` broker refuses to start on Windows. Windows host configuration snippets do not enable or imply runtime support.

Local Laya-MLX remains limited to a compatible Apple-Silicon Mac with a successful local installation attestation. It is not a Windows or Linux provider route. Hosted and local routes are separate choices; Jev-only does not silently fall back to Laya.

## Install

Every host runs the same runtime from the same portable source at `plugins/qualixar-jev-decision-layer`. The install, upgrade and confirm steps for each host live in one place, the README's [Install and upgrade](../README.md#install-and-upgrade) section, so that they cannot drift apart. This section records what is specific to each host. Every host needs Python 3.11 or newer at `/opt/homebrew/bin/python3` or `/usr/local/bin/python3`.

### Codex Desktop

Install from this repository's **Qualixar Jev Layer** marketplace, then start a new task: [README → Codex](../README.md#codex). Upgrade with `codex plugin marketplace upgrade` after quitting Codex Desktop, not while a task is running. The Codex session hook announces that Jev is enrolled but does not state `data_classification`; the [AGENTS.md section](WORKING_WITH_JEV.md#codex-agentsmd) fills that gap.

### Claude Code

Install and upgrade: [README → Claude Code](../README.md#claude-code). Upgrade with `claude plugin update`, not `claude plugin install`, which leaves an installed plugin unchanged.

Adds seven commands — `/jev-setup`, `/jev-status`, `/jev-route`, `/jev-recipes`, `/jev-review`, `/jev-selftest`, `/jev-vscode` — plus the shared skills and the `qualixar-jev` MCP server.

**Hooks.** The plugin registers `SessionStart`, `UserPromptSubmit` and `SubagentStart`; none of them can grant or deny a tool call, and `PreToolUse` is not registered. In an enrolled workspace the guidance names the exact `workspace_path`, `data_classification` and `provider` the grant accepts, and lists the typed tools only when the grant enables generic typed queries. `SubagentStart` returns the same guidance as `hookSpecificOutput.additionalContext`, because a subagent does not inherit SessionStart context. A folder with no active grant, including one whose grant expired, gets one line at session start that says so and tells the model not to enroll it. A folder you revoked or refused stays silent.

**Missing `workspace_path`.** When Claude Code spawns the server with `CLAUDE_PROJECT_DIR` set and a tool call omits `workspace_path`, the server uses that directory. Every advertised schema still requires the argument, and hosts that do not set the variable are unaffected. The server cannot tell which host exported the variable: if you export `CLAUDE_PROJECT_DIR` globally, any host that passes its environment to the server gets the same fallback. Consent is still checked on the resolved folder.

**Tool names.** Plugin tools are `mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__<tool>`. A server registered for the desktop app, or with `claude mcp add`, is named `qualixar-jev` and its tools are `mcp__qualixar-jev__<tool>`. Permission rules and `CLAUDE.md` sections must use the form that matches the install.

**Where the MCP server loads.** Plugin-provided MCP servers are read by the Claude Code CLI and by on-machine Cowork sessions. In the project's tests they were **not** loaded by the desktop app's Code tab: in a Code tab session, every enabled plugin that ships an MCP server was equally absent, with no error and no failed entry. Commands and skills load normally there. The desktop app does load servers from its own `claude_desktop_config.json` into both Chat and local Code tab sessions, so one [desktop registration](#claude-desktop-app) gives the tools to both. Note that `claude mcp list` reports on the CLI's own configuration and says nothing about what the desktop app can see — a green line there is not evidence the app loaded anything.

### VS Code

```sh
plugins/qualixar-jev-decision-layer/scripts/jev vscode --workspace .
```

This writes nothing. It prints the planned change, the config path, and `preserved_servers` — your existing servers, which are kept. On supported macOS, add `--write` to apply. Linux registration code is experimental and unverified; Windows hosted runtime is disabled in this release. An existing `.vscode/mcp.json` is merged: exactly one `qualixar-jev` entry is added or updated and every other key is carried through. A file that does not parse is refused rather than overwritten, because rewriting it would discard servers the adapter cannot read. Restart VS Code afterwards; Copilot agent mode reads the workspace file. The entry points at the launcher that ran the command. From a marketplace install, run it from the installed copy as the [README](../README.md#vs-code-copilot-agent-mode) shows; that copy is a versioned folder, so run the command again after each upgrade.

### Antigravity

Use the portable plugin source at `plugins/qualixar-jev-decision-layer` with Antigravity's own plugin install path. It picks up the root `hooks.json` PreInvocation advisory. For the decision tools, register the MCP server in Antigravity's global config:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev host-register --host antigravity
```

On supported macOS, add `--write` to apply, and repeat it after each upgrade. Linux registration code is experimental and unverified. Windows is outside the supported runtime in this release. A plugin-relative `mcp_config.json` is deliberately **not** shipped: Antigravity documents `command` as an executable or a binary name and says nothing about resolving a path relative to the plugin, so the adapter uses an absolute one in the documented global config. A repository test keeps that file from being added until the relative form is documented and verified.

### Hermes

Use the portable plugin source with Hermes's own plugin install path; it uses separate hook and tool entry points and an explicit tool allow-list. Hermes does not pass a working directory to `pre_llm_call`; the hook uses the same order Hermes does — its session working directory, then `TERMINAL_CWD`, then the launch directory — so worktree and messaging-gateway sessions resolve the workspace Hermes is working in. The source includes offline self-test, `jev_verify`, and `jev_rerank` in that allow-list. A host manifest or tool-list handshake does not prove a native model/tool turn.

### Claude desktop app

Register from the installed Claude Code plugin, with the three lines in [README → Claude desktop app](../README.md#claude-desktop-app-chat-and-the-code-tab): the first picks the newest release in the plugin cache, the second previews, the third writes. `host-register` registers whichever copy of the launcher runs it, so running it from a clone registers the clone.

**Quit the app first.** It holds its config in memory and flushes it on exit, so an edit made while it is running is silently discarded — measured, not assumed. Re-run the command after every upgrade: the plugin cache path carries the release number, and Claude Code deletes the previous release's folder 14 days after an update, so an entry that is not refreshed stops working then. `host-register` warns when the launcher is inside Documents, Desktop or Downloads, where macOS may not let the app run it. An entry that points at an older release of the same launcher in the same cache is replaced; any other existing `qualixar-jev` entry, including one with your own `env` or `args`, or a newer release, is refused as `HOST_MCP_ENTRY_CONFLICT`. This applies to every host `host-register` supports. Windows runtime commands are disabled in this release; do not use a generated configuration snippet as an indication of platform support. `host-register` prints the quit-the-app reminder only when it actually changed the file.

### Enterprise-managed Claude Code

An organization can manage Claude Code with [managed settings](https://code.claude.com/docs/en/managed-settings). These can stop the Jev plugin from running on its own in Claude Code:

| Managed setting | What happens to Jev |
|---|---|
| `allowManagedHooksOnly` (any value except `false`) | Plugin hooks do not run, so sessions get no automatic guidance, unless the plugin is force-enabled in managed `enabledPlugins` |
| `disableAllHooks: true` | No hooks run at all; force-enabling does not help |
| `enabledPlugins` sets `qualixar-jev-decision-layer@qualixar` to `false` | The plugin is turned off |
| `strictKnownMarketplaces` (or `allowedMarketplaces`) without the source you installed from | The plugin, its `/jev-*` commands, skills, hooks and MCP server do not load |
| `blockedMarketplaces` matching that source | The same; the blocklist is checked first |
| `allowedMcpServers` without this install's launcher command | Claude Code does not start the plugin's MCP server |
| `deniedMcpServers` naming `plugin:qualixar-jev-decision-layer:qualixar-jev` or the launcher | The MCP server is blocked |
| a `managed-mcp.json` file that does not define `qualixar-jev` | Only the organization's MCP servers load |

Your Jev grant is unaffected, and so are Codex, VS Code, Antigravity and Hermes.

**How Jev tells you.** `jev doctor` adds a `claude_code_policy` check with status `NOTICE`, `jev_auto_status` adds a `claude_code_policy` field for an enrolled workspace, and step 1 of the setup wizard shows a short notice. On a computer without such a policy none of them appear. The check is read-only. It reads the sources Claude Code documents, applies Claude Code's documented rules (the first managed source that sets a policy key decides unless it opts into merging, drop-in files merge into `managed-settings.json`, an unreadable lock counts as on), and reports fixed codes, a label and location for each source in force, and fixed advice. It never repeats any other policy value. Claude Code has the final word: `/status` shows the managed source in force, `/plugin` shows a blocked marketplace, and `claude doctor` lists policy problems.

When the plugin cannot load at all, none of these run inside Claude Code. The symptom is simple: no `/jev-*` commands appear. Check `/plugin` and `/status` first.

**What an administrator can change.** Allow the marketplace, register it, and force-enable the plugin. Hooks of a force-enabled plugin still run under `allowManagedHooksOnly`. Add these entries to the source Claude Code applies (the one `/status` names), and merge them into the organization's existing lists rather than replacing them:

```json
{
  "strictKnownMarketplaces": [
    { "source": "github", "repo": "qualixar/jev-decision-layer" }
  ],
  "extraKnownMarketplaces": {
    "qualixar": { "source": { "source": "github", "repo": "qualixar/jev-decision-layer" } }
  },
  "enabledPlugins": { "qualixar-jev-decision-layer@qualixar": true },
  "allowedMcpServers": [
    { "serverCommand": ["${HOME}/.claude/plugins/cache/qualixar/qualixar-jev-decision-layer/1.0.13/scripts/launch-jev"] }
  ]
}
```

Force-enabling turns the plugin on for everyone the policy covers. The `allowedMcpServers` entry is needed only if the organization sets that list. Measured with Claude Code 2.1.229: a plugin's MCP server is admitted only by a `serverCommand` equal to the command it runs, with no extra arguments; a `serverName` entry does not match it. The cached path carries the release number, so the entry must be updated with each Jev release, and it differs if users set `CLAUDE_CONFIG_DIR`. `jev doctor` prints the exact command for the install it runs from.

**What you can do meanwhile, if your organization permits it.** Never work around a deliberate block: if the plugin is turned off, the marketplace is blocklisted, or the server is denied, ask whether Jev can be approved.

- *Claude desktop app.* Quit the app, register the tools with the command in [Claude desktop app](#claude-desktop-app), and reopen it. If the plugin could not be installed, use a clone of this repository outside Documents, Desktop and Downloads (for example `~/.local/share/qualixar/jev-decision-layer`), because macOS may not let the app run a program inside those folders; `host-register` warns when it sees one.
- *Claude Code CLI.* If only hooks are blocked, the plugin's tools still load. If the plugin cannot load and no MCP restriction applies, run `claude mcp add qualixar-jev -- <clone>/plugins/qualixar-jev-decision-layer/scripts/launch-jev`.

Then add this section to your user `CLAUDE.md` (`~/.claude/CLAUDE.md`, or `CLAUDE.md` in the folder `CLAUDE_CONFIG_DIR` points to). It does the job the session hook would have done. Replace the two placeholders with your grant's values: `data_classification` is `public` for Jev public, `internal-minimized` for Jev internal or Jev + Laya, and `restricted` for Jev maximum or Laya only. Under Jev + Laya, also tell Claude to pass `restricted` for private or client content (with `provider` `laya-mlx` for `jev_typed_decide`): that is what keeps it on Laya.

```markdown
## Qualixar Jev Decision Layer

My organization's Claude Code policy blocks plugin hooks, so Jev's session guidance does not arrive on its own. Its MCP tools work. They are named `mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__<tool>` when they come from the plugin, and `mcp__qualixar-jev__<tool>` when registered with host-register or `claude mcp add`. Follow this section instead.

- `workspace_path`: the current working directory, as an absolute path. Only folders my Jev grant covers work; elsewhere Jev returns `WORKSPACE_NOT_ENROLLED`. Do not call it there. Never create or widen a grant yourself; open `jev_setup` only when I ask.
- `data_classification`: `<public | internal-minimized | restricted>`
- `provider` (only `jev_typed_decide` takes it): `<typesafe | openrouter | laya-mlx>`
- Never put keys or secrets in any field; they are rejected. Under Jev public, email addresses and home-folder paths are rejected too.

When a bounded choice comes up, ask Jev instead of re-reasoning it:

| Situation | Tool | Arguments to get right | Branch on |
|---|---|---|---|
| Pick one of 2–12 options (task, tool or skill) | `jev_route` | `kind` is `task`, `tool` or `skill`; each candidate is exactly `{"id": ..., "description": ...}`, ids without dots or colons | the selected id; `unknown` means use your own judgment |
| Does an extraction match its source | `jev_verify` | `source_text` and an `extraction` object | `trustworthy`, never an empty suspect list |
| Do retrieved passages answer the question | `jev_rerank` | `memories` is a list of objects with a `content` field | `should_abstain` |
| Where to focus a code review | `jev_review_diff` | the diff text | advisory focus only |
| Which files matter for a narrow task | `jev_prepare` | a short `goal` | the shortlist |
| A bounded check from the recipe catalog | `jev_recipe_try` | a `recipe_id` from `jev_recipe_catalog` and its `input` | the gated result; `verify` means check it yourself |
| Any other bounded, typed question | `jev_typed_decide` | `questions` in the shape the tool schema describes | the typed answer and its confidence |

Answers are advisory. They never replace judgment, the permission prompt, tests or a completion check. If a Jev call errors, continue without it and say so in one line. Subagents load this file too.
```

`jev_route`, `jev_verify`, `jev_rerank`, `jev_review_diff`, `jev_recipe_try` and `jev_typed_decide` need generic typed queries turned on in your grant (the wizard's **Advisory Jev tools** choice); without it they return `GENERIC_QUERY_NOT_ENROLLED`.

**For teams handling client or confidential data.** Hosted Jev sends the reviewed text of each decision to the provider you chose (TypeSafe or OpenRouter), which are separate processors. Get your organization's approval for that provider before using hosted modes on such data, or use Laya only, which keeps decisions on the device. See [SECURITY.md](SECURITY.md).

## What is verified, and what is not

"Evidence" means something was run and produced the stated result. "Boundary" is what that evidence does *not* establish. No row here claims measured token, cost or time savings, because none has been measured.

| Harness | Evidence | Boundary |
|---|---|---|
| **Codex** | Installed MCP tools and live synthetic TypeSafe routing verified on macOS. The Codex hook uses the shared enrollment binding, including an approved root grant over a nested repository, in process | Does not establish a native Codex hook firing, Linux operation, or Windows runtime support. `native_status` remains `NOT_RUN` |
| **Claude Code** | Plugin installs from the repo marketplace and `claude plugin validate` passes; seven commands and three skills load; the MCP launcher answers an `initialize` handshake; the hook launcher exits cleanly and follows the shared enrollment binding for an approved root; in process, the hook emits argument-bearing guidance for each consent state, including `SubagentStart` | Hook output is pinned by in-process tests, not by a live session's model context. A native MCP tool turn inside a live session, and hook firing under a host that permits plugin hooks, are not yet verified. In the desktop app's Code tab the plugin-provided server does not load at all. `native_status` remains `NOT_RUN` |
| **Hermes** | The staged source registers twelve declared tools including self-test, verify and rerank, plus its hook. The hook follows the shared enrollment binding | Native model/tool turn still needs verification. `native_status` remains `NOT_RUN` |
| **Antigravity** | Packaged PreInvocation advisory hook and skills; this adapter does not request PreToolUse authority. The hook follows the shared enrollment binding | Native model/tool turn and portable MCP registration still need verification. `native_status` remains `NOT_RUN` |
| **VS Code** | Adapter writes a valid `.vscode/mcp.json` against the documented `servers` format; merge, refusal and symlink behaviour covered by tests. The registered server uses the shared enrollment binding | No live Copilot agent-mode turn has been run. No extension ships; registration is the whole integration. `native_status` remains `NOT_RUN` |

`native_adapter` in the host inventory means this package ships a shim for that harness. It is derived from files in this repository and never from probing a host, and it is not a conformance claim — `native_status` stays `NOT_RUN` for every row until someone runs one.

## Check it before you spend anything

Every recipe ships three synthetic cases. Replaying all 165 through the real gate costs no provider call, no key, and no workspace enrolment:

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
