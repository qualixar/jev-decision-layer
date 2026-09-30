# Remove or roll back

Removal is per host, because each harness installs the plugin its own way. Revoking a workspace policy is separate from uninstalling, and either can be done without the other.

## Stop future calls immediately

Revoking the workspace policy stops Jev calls for that workspace without touching the installation:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev revoke --workspace .
```

Ask the agent to help you identify the exact workspace path rather than deleting a broad state directory. Existing native host use is unchanged by this.

## Uninstall

### Codex

```sh
codex plugin remove qualixar-jev-decision-layer@qualixar-jev-layer
codex plugin marketplace remove qualixar-jev-layer
```

### Claude Code

```sh
claude plugin uninstall qualixar-jev-decision-layer@qualixar
claude plugin marketplace remove qualixar
```

If you registered Jev for the Claude desktop app, remove that entry too — it is separate from the plugin and survives uninstalling it. Quit the app first, then delete the `qualixar-jev` block from `~/Library/Application Support/Claude/claude_desktop_config.json` and reopen the app. If you ever added it with `claude mcp add`, also run:

```sh
claude mcp remove qualixar-jev
```

### VS Code

The adapter added exactly one server to `.vscode/mcp.json`. Delete the `qualixar-jev` entry from the `servers` object and leave the rest of the file alone. Nothing else was written.

### Antigravity and Hermes

Remove the plugin through the host's own plugin install path. If you registered the tools for Antigravity with `host-register`, quit Antigravity and delete the `qualixar-jev` entry from `mcpServers` in `~/.gemini/config/mcp_config.json`.

## What removal does not do

Uninstalling removes the installed plugin and its marketplace registration. It does **not** delete your project, an older plugin's source, workspace policies, local receipts, a local Laya install (`mlx-env/` and `laya-models/` under `~/.local/state/qualixar-jev-decision-layer/`), or macOS Keychain items (`ai.qualixar.adl.typesafe`, `ai.qualixar.adl.openrouter`). Delete those yourself, one named item at a time, if you no longer need them. Start a new task in the host afterwards so the removed skill and command text stops loading.

If you are replacing an older, separately installed Jev integration, keep it available until this plugin passes a fresh task in your harness. This plugin uses its own state and socket namespace and never silently reads an older key file. A failed upgrade should leave the older plugin and its data recoverable — do not remove both at once.
