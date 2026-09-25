# First use

The setup path is the same on every supported harness: install the plugin, ask the agent to set up the workspace, review the scope in a private local wizard, then confirm the provider with one synthetic decision. Per-host install commands and what differs between harnesses are in [the host guide](HOSTS.md).

Guided hosted setup currently requires macOS Keychain; optional Laya additionally needs a verified Apple-Silicon macOS installation.

## Try it before you enrol

You do not need a key, a provider, or an enrolled workspace to see whether the layer behaves. Every recipe ships three synthetic cases — a clear-cut one, a genuinely ambiguous one, and one carrying an instruction hidden in material that is supposed to be data:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest
```

That replays all 108 through the real gate offline and reports counts. From a host with the MCP tools, `jev_recipe_selftest` does the same, and `jev_recipe_selftest` with a `recipe_id` shows one case with expected against observed. This is a contract check on the shipped gate, never evidence of provider accuracy.

## Enrol a workspace

After installing, start a new task and say: **"Set up Qualixar Jev Decision Layer for this workspace."** The agent calls `jev_setup`, which opens a private local two-step wizard for that exact project folder. No terminal command is needed and you never paste a key into chat. The same action later opens a reviewed scope-upgrade flow rather than silently widening an existing grant.

On the first screen, select a decision mode: Jev public, Jev reviewed internal, Jev maximum (reviewed workspace text to the chosen hosted provider), Jev + Laya hybrid, or Laya-only. TypeSafe is the direct hosted Jev route; OpenRouter is separate. Laya modes are available only after the local installation passes attestation, and the local route stays off until you turn it on — Jev-only never silently switches to Laya. Jev maximum can include client or confidential text only if you are permitted to share it; the extra confirmation is not permission from the data owner. Secret screening is best-effort, not comprehensive DLP. Advanced controls set consent duration, daily attempts and bytes. Explicit generic Jev tools start on after reviewed setup; automatic prompt guidance starts **off** and must be turned on explicitly. Laya-only makes no automatic Jev call. Prompt guidance sends only bounded prompt terms and candidate titles; it does not run on every turn or make a tool call on your behalf.

On the review screen, read the full folder path, provider, data scope, budgets, and generic-query setting. Enter a hosted API key in the password field — or leave it blank only if that provider's key is already in macOS Keychain — and tick the confirmation box yourself. No live call runs just because you open or review the wizard. Laya-only needs no hosted key; hybrid still needs a key for its Jev route. Reopening `jev_setup` on an enrolled workspace shows the current and proposed scope; the same provider key stays in Keychain, and a concurrent policy change or missing review blocks the upgrade.

## Confirm it works

Let the host present any native hook-trust prompt. Inspect and trust only the exact current Qualixar hook definition if you want automatic guidance; installing a plugin does not trust its hooks. Some harnesses, or an organization's managed settings, may not permit plugin hooks at all — the MCP tools and commands are unaffected either way.

In a fresh task, ask for `jev_auto_status` on the workspace. That readiness check makes no model request. To confirm the actual provider, ask for one synthetic decision and check the returned provider, model and receipt ID. An offline fixture result is labelled simulated and does not verify provider access — never read one as proof that a live route works.

Do not create an `.env` file as a workaround for the Keychain wizard; `.env.example` is a developer reference, not the ordinary setup path.

## If setup fails

Keep normal work going in the host; the layer is advisory and its absence blocks nothing. Do not paste a key into a bug report. The private local policy can be revoked and the plugin removed without deleting the project — see [rollback](ROLLBACK.md). An older Jev installation is not silently imported: the new plugin keeps separate local state and asks you to review its workspace scope once.

## Update an existing marketplace installation

Finish any active Codex task and fully quit Codex Desktop before updating from your system terminal. A running task may still call a hook from the old version's cache after Codex replaces that cache, producing a `can't open file .../hooks/jev_hook.py` error even when the new version installed successfully. Then refresh the marketplace and installed plugin:

```sh
codex plugin marketplace upgrade qualixar-jev-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

Reopen Codex Desktop and start a fresh task. Check the installed version in the Plugins screen or with `codex plugin list`. If the old task showed the missing-hook-file error, reopen the app first and check the installed version before retrying the update; do not re-enter your provider key to fix a missing local file. A workspace's reviewed consent is separate from the plugin files.

On Claude Code:

```sh
claude plugin marketplace update qualixar
claude plugin install qualixar-jev-decision-layer@qualixar
```

Neither re-enters a saved key or widens an enrolled workspace's consent. A host may require a fresh review if an update changes a hook definition; do not bypass that native review.
