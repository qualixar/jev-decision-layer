# First use in Codex Desktop

Guided hosted setup currently requires macOS Keychain; optional Laya additionally needs a verified Apple-Silicon macOS installation. Install the plugin from this repository's **Qualixar Jev Layer** marketplace in Codex Desktop, then start a new task. Tell Codex: “Set up Qualixar Jev Decision Layer for this workspace.” Codex can call `jev_setup` to open the private local two-step setup wizard for the exact project folder; no terminal command is needed. You do not need to paste a key into Codex chat. The same action opens a reviewed scope-upgrade flow later without silently widening an existing grant.

On the first screen, select a decision mode: Jev public, Jev reviewed internal, Jev maximum (reviewed workspace text to the chosen hosted provider), Jev + Laya hybrid, or Laya-only. TypeSafe is the direct hosted Jev route; OpenRouter is separate. Laya modes are available only after the local installation passes attestation. Jev-only does not silently switch to Laya. Jev maximum can include client or confidential text only if you are permitted to share it; the extra confirmation is not permission from the data owner. Secret screening is best-effort, not comprehensive DLP. The Advanced controls let you change the consent duration, daily attempts and bytes. Explicit generic Jev tools start on after reviewed setup; automatic prompt guidance starts **off** and must be turned on explicitly. Laya-only makes no automatic Jev call. Prompt guidance sends only bounded prompt terms and candidate titles; it does not run on every turn or make a tool call on your behalf.

On the review screen, read the full folder path, provider, data scope, budgets, and generic-query setting. Enter a hosted API key in the password field—or leave it blank only if that provider's key is already in macOS Keychain—and tick the confirmation box yourself. No live call runs just because you open or review the wizard. Laya-only needs no hosted key; hybrid still needs a key for its Jev route. If you later ask Codex to change an existing workspace scope, `jev_setup` reopens the same private wizard and shows the current and proposed scope. The same provider key stays in Keychain; a concurrent policy change or missing review blocks the upgrade.

After saving, let Codex present any native hook-trust prompt. Inspect and trust only the exact current Qualixar hook definition if you want automatic guidance; installing a plugin does not trust its hooks. In a fresh task, ask for `jev_auto_status` on the workspace; that readiness check makes no model request. To check the actual provider, ask for one synthetic decision and confirm the returned provider/model and receipt. An offline `jev_run_fixture` result is labeled simulated and does not verify provider access. Do not create an `.env` file as a workaround for the Keychain wizard; `.env.example` is a developer reference, not the ordinary setup path.

If setup fails, keep normal Codex work going. Do not paste a key into a bug report. The private local policy can be revoked and the plugin removed without deleting the project. An older Jev installation is not silently imported: the new plugin keeps a separate local state and asks you to review its workspace scope once.

## Update an existing GitHub marketplace installation

For a maintenance change to the repository's `main` branch, refresh the marketplace and installed plugin, then start a fresh Codex task so the changed skill text loads:

```sh
codex plugin marketplace upgrade qualixar-jev-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

This does not re-enter a saved TypeSafe key or widen an enrolled workspace's consent. Codex may require a fresh review if a future update changes a hook definition; do not bypass that native review. This browser-skill maintenance change kept the existing hook definitions unchanged.
