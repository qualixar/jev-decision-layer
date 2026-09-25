# Remove or roll back the Codex plugin

Remove **Qualixar Jev Decision Layer** from the Codex Plugins Directory and start a new task. For a developer using the CLI, the following removal sequence was tested in an isolated Codex home:

```sh
codex plugin remove qualixar-jev-decision-layer@qualixar-jev-layer
codex plugin marketplace remove qualixar-jev-layer
```

This removes the installed plugin and its marketplace registration. It does not delete your project, an older plugin's source, workspace policies, local receipts, or macOS Keychain items. To stop future Jev calls for an enrolled workspace immediately, revoke that workspace's policy before or after uninstalling the plugin; ask Codex to help you identify the exact workspace rather than deleting a broad state directory.

If you are replacing an older Jev installation, keep it available until the new plugin passes a fresh Codex task. The new 1.0.0 plugin uses its own state and socket namespace and never silently reads an older key file. A failed upgrade should leave the older plugin and its data recoverable; do not remove both at once.
