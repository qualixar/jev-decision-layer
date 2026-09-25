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

If you also registered the launcher directly for the desktop app's Code tab, remove that entry too — it is separate from the plugin and survives uninstalling it:

```sh
claude mcp remove qualixar-jev
```

and delete the matching `qualixar-jev` block from `~/Library/Application Support/Claude/claude_desktop_config.json` if you added one there.

### VS Code

The adapter added exactly one server to `.vscode/mcp.json`. Delete the `qualixar-jev` entry from the `servers` object and leave the rest of the file alone. Nothing else was written.

### Antigravity and Hermes

Remove the plugin through the host's own plugin install path.

## What removal does not do

Uninstalling removes the installed plugin and its marketplace registration. It does **not** delete your project, an older plugin's source, workspace policies, local receipts, or macOS Keychain items. Start a new task in the host afterwards so the removed skill and command text stops loading.

If you are replacing an older Jev installation, keep it available until the new plugin passes a fresh task in your harness. Version 1.0.1 uses its own state and socket namespace and never silently reads an older key file. A failed upgrade should leave the older plugin and its data recoverable — do not remove both at once.
