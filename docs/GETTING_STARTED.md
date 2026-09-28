# First use

The shortest useful path is: **install the host adapter → run the offline diagnosis → review one workspace's provider and text scope → run one synthetic bounded decision → inspect its returned status and receipt.** For `jev_recipe_try`, also inspect `host_action` and `policy_receipt_id`; those outputs remain advisory. The host retains its normal permissions. Host install and verification details differ, so use [the host guide](HOSTS.md) for the exact surface and its current evidence.

The 1.0.8 release candidate targets hosted TypeSafe Jev and OpenRouter on macOS, Linux, and Windows, using each operating system's credential store. Native Windows and Linux CI must pass before those platform paths are treated as verified. Windows host registration is manual-plan-only and `--write` fails closed. Optional local Laya-MLX is supported only on a compatible Apple-Silicon Mac after local attestation. Host evidence is tracked separately; a packaged adapter does not prove a live provider turn.

## Try it before you enrol

You do not need a key, a provider, or an enrolled workspace to see whether the layer behaves. Every recipe ships three synthetic cases — a clear-cut one, a genuinely ambiguous one, and one carrying an instruction hidden in material that is supposed to be data:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest
```

That replays all 114 through the real gate offline and reports counts. From a host with the MCP tools, `jev_recipe_selftest` does the same, and `jev_recipe_selftest` with a `recipe_id` shows one case with expected against observed. This is a contract check on the shipped gate, never evidence of provider accuracy.

## Install

Add the marketplace once, then install the plugin from it. Neither command needs a clone of this repository — both read it from GitHub.

On Codex Desktop:

```sh
codex plugin marketplace add qualixar/jev-decision-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

On Claude Code:

```sh
claude plugin marketplace add qualixar/jev-decision-layer
claude plugin install qualixar-jev-decision-layer@qualixar
```

Both packages use the shared runtime, while the host's plugin format, hooks, registration and available capabilities differ. The five adapters do not have identical surfaces or equal native evidence. Per-host detail is in [the host guide](HOSTS.md). On Windows, `jev host-register --write` fails closed for all targets; use its planned instructions as a manual, reviewed configuration step.

If the plugin is already installed and you are moving to a newer version, use the upgrade steps below instead — they quit the host first, which a fresh install does not need.

## Check the workspace before private setup

From a local clone of this repository, run the read-only diagnosis against the project folder where you plan to use Jev:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev doctor --workspace /absolute/path/to/your/project
```

`jev doctor` is **offline-only** and makes no provider call. It checks runtime integrity, the Python requirement, workspace policy, offline gate fixtures, the local receipt index, and whether the portable runtime is present. Before enrollment, `ACTION_REQUIRED` with `NOT_ENROLLED` and exit status `2` are expected. `PORTABLE_RUNTIME` reports the local package surface; it does not prove the host loaded the plugin or completed a live turn. A marketplace install alone does not add a global `jev` shell command, so use the local checkout for this CLI check.

## Enrol a workspace

After installing, start a new task and say: **"Set up Qualixar Jev Decision Layer for this workspace."** The agent calls `jev_setup`, which opens a private local two-step wizard for that exact project folder. No terminal command is needed and you never paste a key into chat. The same action later opens a reviewed scope-upgrade flow rather than silently widening an existing grant.

On the first screen, select a decision mode: Jev public, Jev reviewed internal, Jev maximum (reviewed workspace text to the chosen hosted provider), Jev + Laya hybrid, or Laya-only. TypeSafe is the direct hosted Jev route; OpenRouter is separate. Laya modes are available only after the local installation passes attestation, and the local route stays off until you turn it on — Jev-only never silently switches to Laya. Jev maximum can include client or confidential text only if you are permitted to share it; the extra confirmation is not permission from the data owner. Secret screening is best-effort, not comprehensive DLP. Advanced controls set consent duration, daily attempts and bytes. Explicit generic Jev tools start on after reviewed setup; automatic prompt guidance starts **off** and must be turned on explicitly. Laya-only makes no automatic Jev call. Prompt guidance sends only bounded prompt terms and candidate titles; it does not run on every turn or make a tool call on your behalf.

On the review screen, read the full folder path, provider, data scope, budgets, and generic-query setting. Enter a hosted API key in the password field — or leave it blank only if that provider's key is already in the operating system's credential store — and tick the confirmation box yourself. No live call runs just because you open or review the wizard. Laya-only needs no hosted key; hybrid still needs a key for its Jev route. Reopening `jev_setup` on an enrolled workspace shows the current and proposed scope; the provider key remains in the OS credential store, and a concurrent policy change or missing review blocks the upgrade.

## Confirm it works

Let the host present any native hook-trust prompt. Inspect and trust only the exact current Qualixar hook definition if you want automatic guidance; installing a plugin does not trust its hooks. Some harnesses, or an organization's managed settings, may not permit plugin hooks at all — the MCP tools and commands are unaffected either way.

In a fresh task, ask for `jev_auto_status` on the workspace. That readiness check makes no model request. To confirm an actual provider route, ask:

> Route this synthetic duplicate-charge request between `billing` and `engineering`. Show the selected candidate, provider, model, route status, and receipt ID.

Check that the response identifies the provider and model, reports its route status, and includes a receipt ID. The selection may vary with the provider answer. An offline fixture result is labelled simulated and does not verify provider access — never read one as proof that a live route works. A receipt records evidence about a decision; it does not prove that the suggested business action was executed or correct.

You can also use the existing `jev probe --workspace /absolute/path/to/your/project` command after setup to make a separate explicit provider probe. It is provider activity, not part of `jev doctor`; do not treat the offline diagnosis as evidence of a provider turn.

For `jev_recipe_try` specifically, the 1.0.8 live response is marked `EXPERIMENTAL_ADVISORY` and includes `host_action` and `policy_receipt_id`. Every shipped recipe is `SPECIFICATION_NOT_MODEL_EVALUATED`, so a result that would otherwise be `act` is capped to `verify`; an explicit `unknown` may return `ignore`. The host still makes and executes any authorized choice. The policy receipt records the gate outcome; it does not establish provider accuracy.

Do not create an `.env` file as a workaround for the OS credential-store setup; `.env.example` is a developer reference, not the ordinary setup path.

## Let a non-developer explore a recipe

The local recipe workbench provides a form-based view for a manager, creator, or junior teammate who wants to understand a decision without composing MCP JSON. Start it from a local checkout:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev workbench --workspace /absolute/path/to/your/project
```

The workbench opens on loopback and begins in offline mode. Choose a recipe, inspect its question and input fields, and run a synthetic fixture without a provider call. To try a live answer, first complete the workspace's normal reviewed setup, then explicitly select the live review flow and inspect the provider, text scope, gate status, and receipts. Live recipe output is marked `EXPERIMENTAL_ADVISORY`; each would-be passing result is capped to `verify` while the recipe is `SPECIFICATION_NOT_MODEL_EVALUATED`. The workbench never executes a recommendation. Close it when finished; its local session and review controls are designed for this short-lived use.

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
