# First use

The shortest useful path is: **install the host adapter → try the offline self-test → choose Jev, Laya, or Jev + Laya → approve one folder → run one synthetic bounded decision → inspect its status and receipt.** For `jev_recipe_try`, also inspect `host_action` and `policy_receipt_id`; those outputs remain advisory. The host retains its normal permissions. Host install and verification details differ, so use [the host guide](HOSTS.md) for the exact surface and its current evidence.

Release 1.0.13 is supported and verified on macOS. Linux remains experimental and unverified; Windows runtime entry points refuse to start because the native private-state contract did not pass CI. Local Laya runs only on an Apple-Silicon Mac with macOS 14 or later, after [`jev laya-install`](#set-up-local-laya) has verified it. Host evidence is tracked separately; a packaged adapter does not prove a live provider turn.

## Before you start

You need a Mac and **Python 3.11 or newer** at `/opt/homebrew/bin/python3` or `/usr/local/bin/python3` (Homebrew, or the python.org installer). The launchers look only there and in `/usr/bin/python3`, which on macOS is 3.9 and too old; with no suitable Python every `jev` command prints `PYTHON_3_11_REQUIRED`, and the tools never appear in your host. The full list, including what hosted Jev and local Laya each need, is in the [README](../README.md#before-you-install). claude.ai in a web browser is not supported: Jev runs as a program on your computer, and a browser session cannot start one.

## Try it before you enrol

You do not need a key, a provider, or an enrolled workspace to see whether the layer behaves. Every recipe ships three synthetic cases — a clear-cut one, a genuinely ambiguous one, and one carrying an instruction hidden in material that is supposed to be data:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest
```

That replays all 165 through the real gate offline and reports counts. From a host with the MCP tools, `jev_recipe_selftest` does the same (in Claude Code, `/jev-selftest`), and `jev_recipe_selftest` with a `recipe_id` shows one case with expected against observed. This is a contract check on the shipped gate, never evidence of provider accuracy.

## Install

Use the install section for your host in the README: [Claude Code](../README.md#claude-code), [the Claude desktop app](../README.md#claude-desktop-app-chat-and-the-code-tab), [Codex](../README.md#codex), [VS Code](../README.md#vs-code-copilot-agent-mode), [Antigravity](../README.md#antigravity) or [Hermes](../README.md#hermes). Neither marketplace install needs a clone of this repository. Both packages use the shared runtime, while the host's plugin format, hooks, registration and available capabilities differ. The five adapters do not have identical surfaces or equal native evidence. Windows is outside the supported runtime in this release.

If the plugin is already installed and you are moving to a newer version, use [the upgrade steps](#update-an-existing-installation) instead.

## Check the workspace before private setup

From a local clone of this repository, run the read-only diagnosis against the project folder where you plan to use Jev:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev doctor --workspace /absolute/path/to/your/project
```

`jev doctor` is **offline-only** and makes no provider call. It checks runtime integrity, the Python requirement, workspace policy, offline gate fixtures, the local receipt index, and whether the portable runtime is present. Before enrollment, `ACTION_REQUIRED` with `NOT_ENROLLED` and exit status `2` are expected. `PORTABLE_RUNTIME` reports the local package surface; it does not prove the host loaded the plugin or completed a live turn. A marketplace install does not add a global `jev` shell command: use a clone, or the installed copy as shown in the [README](../README.md#claude-desktop-app-chat-and-the-code-tab) (`"$JEV" doctor --workspace …`).

## Choose Jev, Laya, or Jev + Laya

The setup page asks where each decision in the folder is made:

- **Jev** sends the text of each decision to the hosted provider you choose, TypeSafe or OpenRouter. You need an API key from that provider, which bills you under its own terms. Jev has three data levels: public, reviewed internal, and maximum.
- **Laya** decides on your Mac. Nothing leaves the computer and there is no provider bill. It needs the one-time [local Laya setup](#set-up-local-laya).
- **Jev + Laya** sends ordinary decisions to Jev and keeps every decision marked `restricted` (private or client content) on Laya. It needs both a key and the local setup.

For client, customer, personal or contract material, choose **Laya only**, or leave that folder out of Jev. The [README](../README.md#choose-where-decisions-run-jev-laya-or-jev--laya) compares the three, and [Security and data handling](SECURITY.md) says exactly what each sends where.

## Set up local Laya

Skip this if you only want hosted Jev. Laya modes stay greyed out in the setup page until a verified local install exists; the page then says **Laya is not installed yet** and shows the command, with the right path for your install. Run it in Terminal. With a clone of this repository, it is:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev laya-install
```

1. It checks for an Apple-Silicon Mac, macOS 14 or later, Python 3.11 or later, and `git`, and stops with a fixed code (`LAYA_REQUIRES_APPLE_SILICON`, `LAYA_REQUIRES_MACOS_14`, `LAYA_REQUIRES_GIT`, `PYTHON_3_11_REQUIRED`) if one is missing.
2. It tells you what it will download and asks you to type `INSTALL`. It installs the pinned Laya runtime from github.com into Jev's own Python environment, and downloads the pinned model (about 600–800 MB) from huggingface.co.
3. It hashes every model file against its pin, and records the install only after the same check the setup page runs has passed. A file that does not match its pin is refused (`LAYA_WEIGHTS_MISMATCH`).
4. When it prints `Laya is ready`, reopen the setup page and choose **Laya only** or **Jev + Laya**.

Options: `--model multilingual` for the multilingual model; `--model-dir /absolute/path` to use model files already on the computer (a plain folder, or a Hugging Face cache snapshot of the pinned revision) instead of downloading; `--python /absolute/path/to/python3` to choose the interpreter; `--yes` to skip the prompt. An assistant that runs the command for you has no terminal to type `INSTALL` into, so it must pass `--yes`; read what the command downloads before you let it. On a managed network, github.com and huggingface.co must be reachable. Everything is written under `~/.local/state/qualixar-jev-decision-layer/`.

**Using Jev + Laya.** Your assistant marks each request. Ordinary text goes as `internal-minimized` to Jev; private or client content goes as `restricted` and is decided by Laya on this Mac, for `jev_route`, `jev_verify`, `jev_rerank`, `jev_review_diff` and `jev_recipe_try` alike (and for `jev_typed_decide` with `provider: "laya-mlx"`). In Claude Code, the session guidance for a Jev + Laya approval says this to the model. Elsewhere, add it to your instructions ([templates](WORKING_WITH_JEV.md#instruction-templates-for-claudemd-agentsmd-and-copilot)).

## Enrol a workspace

After installing, start a new task and say: **"Set up Qualixar Jev Decision Layer for this workspace."** The agent calls `jev_setup`, which opens a private local two-step wizard for that folder — the current project, or a parent folder you name, such as `/jev-setup ~/Documents` in Claude Code. No terminal command is needed and you never paste a key into chat. The same action later opens a reviewed scope-upgrade flow rather than silently widening an existing grant.

On the first screen, select a decision mode: Jev public, Jev reviewed internal, Jev maximum (reviewed workspace text to the chosen hosted provider), Jev + Laya, or Laya only. TypeSafe is the direct hosted Jev route; OpenRouter is separate. Laya modes are available only after [local Laya setup](#set-up-local-laya), and Jev-only never silently switches to Laya. Jev maximum can include client or confidential text only if you are permitted to share it; the extra confirmation is not permission from the data owner. Secret screening is best-effort, not comprehensive DLP. Advanced controls set consent duration, daily attempts and bytes. The explicit Jev tools (**Advisory Jev tools**) start on after reviewed setup; automatic prompt guidance starts **off** and must be turned on explicitly. Laya only makes no hosted call. Prompt guidance sends only bounded prompt terms and candidate titles; it does not run on every turn or make a tool call on your behalf.

On the review screen, read the full folder path, provider, data scope, budgets, and the Advisory Jev tools setting. Enter the API key from your TypeSafe or OpenRouter account in the password field — or leave it blank only if that provider's key is already in the macOS Keychain — and tick the confirmation box yourself. No live call runs just because you open or review the wizard. Laya only needs no hosted key; Jev + Laya still needs a key for its Jev route. Reopening `jev_setup` on an enrolled workspace shows the current and proposed scope; the provider key remains in the Keychain, and a concurrent policy change or missing review blocks the upgrade.

To cover many projects with one approval, run setup on their parent folder (for example `~/Documents`). The wizard pre-selects child coverage for a folder that is not a Git repository and leaves it off for a single project; either way you confirm it separately on the review screen. That one grant applies in every harness where the plugin is installed and lasts 365 days by default. From a terminal, the equivalent is `jev enroll --workspace /absolute/path/to/parent --cover-descendants --generic-query`, which asks you to type `ENABLE` and then `COVER CHILDREN`, and refuses to run without an interactive terminal. A nested repository with its own consent, or an explicit refusal, keeps that consent. Child coverage can never be attached to your home directory or a filesystem top such as `/Users`.

## Confirm it works

Let the host present any native hook-trust prompt. Inspect and trust only the exact current Qualixar hook definition if you want automatic guidance; installing a plugin does not trust its hooks. Some harnesses, or an organization's managed settings, do not permit plugin hooks. Where only hooks are blocked, the MCP tools and commands still load. A managed marketplace allowlist or blocklist, a disabled plugin, or an MCP server restriction can stop the commands and tools too; [Enterprise-managed Claude Code](HOSTS.md#enterprise-managed-claude-code) lists each setting and what it stops.

In a fresh task, ask for `jev_auto_status` on the workspace. That readiness check makes no model request. To confirm an actual provider route, ask:

> Route this synthetic duplicate-charge request between `billing` and `engineering`. Show the selected candidate, provider, model, route status, and receipt ID.

Check that the response identifies the provider and model, reports its route status, and includes a receipt ID. The selection may vary with the provider answer. An offline fixture result is labelled simulated and does not verify provider access — never read one as proof that a live route works. A receipt records evidence about a decision; it does not prove that the suggested business action was executed or correct.

You can also use the existing `jev probe --workspace /absolute/path/to/your/project` command after setup to make a separate explicit provider probe. It is provider activity, not part of `jev doctor`; do not treat the offline diagnosis as evidence of a provider turn.

For `jev_recipe_try` specifically, the live response is marked `EXPERIMENTAL_ADVISORY` and includes `host_action` and `policy_receipt_id`. Every shipped recipe is `SPECIFICATION_NOT_MODEL_EVALUATED`, so a result that would otherwise be `act` is capped to `verify`; an explicit `unknown` may return `ignore`. Read `recommendation` and `reasons` for what Jev said. The host still makes and executes any authorized choice. The policy receipt records the gate outcome; it does not establish provider accuracy.

Do not create an `.env` file as a workaround for the Keychain setup; `.env.example` is a developer reference, not the ordinary setup path.

[Working with Jev](WORKING_WITH_JEV.md) covers what to ask next, with a first-session playbook for creators, managers and developers.

## Let a non-developer explore a recipe

The local recipe workbench provides a form-based view for a manager, creator, or junior teammate who wants to understand a decision without composing MCP JSON. It is started from a terminal, so someone may need to start it for you:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev workbench --workspace /absolute/path/to/your/project
```

The workbench opens on loopback and begins in offline mode. Choose a recipe, inspect its question and input fields, and run a synthetic fixture without a provider call. To try a live answer, first complete the workspace's normal reviewed setup, then explicitly select the live review flow and inspect the provider, text scope, gate status, and receipts. Live recipe output is marked `EXPERIMENTAL_ADVISORY`; each would-be passing result is capped to `verify` while the recipe is `SPECIFICATION_NOT_MODEL_EVALUATED`. The workbench never executes a recommendation. Close it when finished; its local session and review controls are designed for this short-lived use.

Without the workbench, the same recipes work by asking your assistant in plain words, for example *"Show me the Jev recipes for content creators"*.

## If setup fails

Keep normal work going in the host; the layer is advisory and its absence blocks nothing. Do not paste a key into a bug report. The private local policy can be revoked and the plugin removed without deleting the project — see [rollback](ROLLBACK.md). An older Jev installation is not silently imported: the new plugin keeps separate local state and asks you to review its workspace scope once.

## Update an existing installation

Neither host's update re-enters a saved key or widens an enrolled workspace's consent. A host may require a fresh review if an update changes a hook definition; do not bypass that native review.

**Claude Code.** `claude plugin install` does not upgrade a plugin that is already installed; it reports that the plugin is already installed and changes nothing. Use:

```sh
claude plugin marketplace update qualixar
claude plugin update qualixar-jev-decision-layer@qualixar
```

Then start a new session, or run `/reload-plugins`. **If you registered Jev for the Claude desktop app, quit the app and run the registration again** ([README](../README.md#claude-desktop-app-chat-and-the-code-tab)). Claude Code deletes the previous version's folder 14 days after an update, and an entry that still points there stops working. Re-run `/jev-vscode` or `jev vscode --workspace …` for VS Code, and `jev host-register --host antigravity` for Antigravity, for the same reason.

**Codex.** Finish any active Codex task and fully quit Codex Desktop before updating from your system terminal. A running task may still call a hook from the old version's cache after Codex replaces that cache, producing a `can't open file .../hooks/jev_hook.py` error even when the new version installed successfully. Then refresh the marketplace and installed plugin:

```sh
codex plugin marketplace upgrade qualixar-jev-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

Reopen Codex Desktop and start a fresh task. Check the installed version in the Plugins screen or with `codex plugin list`. If the old task showed the missing-hook-file error, reopen the app first and check the installed version before retrying the update; do not re-enter your provider key to fix a missing local file. A workspace's reviewed consent is separate from the plugin files.

### If a Codex hook shows an error after an upgrade

In Codex, an enabled hook in Settings confirms registration, not that a particular run succeeded. The Hook stats history can retain a failed `SessionStart` from a task that was open during an upgrade. Expand that run to read its command, stderr, and time; then compare it with a new `SessionStart` after restarting Codex. A successful current run does not erase an older failed row. If the new run fails, keep its exact error text for diagnosis. A missing old cache file calls for a restart; `local service unavailable` calls for checking the enrolled workspace and local broker with `jev doctor --workspace /absolute/path/to/project`. Neither error calls for re-entering a provider key or changing the workspace grant.

`hook returned invalid session start JSON output` means Codex rejected that run's stdout as a `SessionStart` response. It does not identify which field or extra output caused the rejection. A Hook stats row labeled `Plugin` does not name the plugin when several are installed; inspect the registered `SessionStart` commands in Hooks before attributing the failure. Codex treats stdout beginning with `{` or `[` as JSON, so bracket-prefixed prose must be wrapped in the event's JSON object or kept off stdout. Validate the exact installed command against Codex's `SessionStart` schema. Other hooks have separate contracts: a `UserPromptSubmit` response with `hookSpecificOutput` must include `hookEventName: "UserPromptSubmit"`. Do not change Jev's enrolled policy to repair an unrelated user hook.
