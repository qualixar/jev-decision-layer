<p align="center"><img src="docs/assets/jev-mark.svg" alt="Qualixar Jev Decision Layer mark" width="56" height="56"></p>

<h1 align="center">Qualixar Jev Decision Layer for AI Agents</h1>

<p align="center"><strong>A fast second opinion for the small, closed choices your AI assistant makes all day.</strong><br>
Which option fits, does this draft meet the brief, is this extracted field right, do these search results answer the question. Each answer comes back as a choice, a score or a yes/no likelihood, with a confidence and a local receipt. It never gives your assistant permission to do anything.</p>

<p align="center">
<a href="https://github.com/qualixar/jev-decision-layer/releases/tag/v1.0.13"><img src="https://img.shields.io/badge/version-1.0.13-7655d9?style=flat-square" alt="Version 1.0.13"></a>
<img src="https://img.shields.io/badge/host%20adapters-5-7655d9?style=flat-square" alt="Five host adapters; verification varies by host">
<img src="https://img.shields.io/badge/MCP%20tools-20-4f46e5?style=flat-square" alt="Twenty MCP tools">
<img src="https://img.shields.io/badge/recipes-55-0f766e?style=flat-square" alt="55 recipes">
<img src="https://img.shields.io/badge/offline%20fixtures-165-d97706?style=flat-square" alt="165 offline fixtures">
<img src="https://img.shields.io/badge/license-MIT-f97316?style=flat-square" alt="MIT license">
</p>

<p align="center"><a href="#what-it-is">What it is</a> · <a href="#choose-where-decisions-run-jev-laya-or-jev--laya">Jev, Laya or both</a> · <a href="#your-first-10-minutes">First 10 minutes</a> · <a href="#install-and-upgrade">Install and upgrade</a> · <a href="docs/WORKING_WITH_JEV.md">Working with Jev</a> · <a href="docs/SECURITY.md">Security</a> · <a href="CHANGELOG.md">Changelog</a></p>

<p align="center"><img src="docs/assets/hero.svg" alt="Qualixar Jev Decision Layer: typed, advisory decisions for Claude Code, Codex, VS Code, Antigravity and Hermes, from hosted Jev, local Laya, or both" width="820"></p>

## What it is

Your AI assistant (Claude, Codex, Copilot and others) makes many small decisions on your behalf: which of three formats suits this post, whether a draft meets the brief, which team a request belongs to, whether a search result really answers the question. Qualixar Jev Decision Layer lets the assistant hand one of those **closed questions** to a small, fast decision model and get back a typed answer: one of the options you gave, a position on a scale you defined, or a yes/no likelihood. Every answer carries a confidence and a local receipt ID, and every answer is advice. Your assistant still decides, and still asks you before it acts.

It is an open-source (MIT) plugin and local MCP server. The decision model is either hosted [TypeSafe Jev](https://docs.typesafe.ai/introduction/coding-agents) (directly or through OpenRouter) or [Laya](https://github.com/mizorewww/laya-mlx), which runs on your own Mac. Nothing is sent anywhere until you approve a folder in a private setup page on your computer.

**Current release: 1.0.13** — 20 MCP tools, 55 recipes for bounded decisions, 165 synthetic offline fixtures, and five host adapters. This release is supported and verified on macOS. Linux is experimental and unverified. Windows is disabled: its runtime entry points refuse to start because the native private-state contract did not pass CI. Local Laya needs an Apple-Silicon Mac. See the [host evidence](docs/HOSTS.md) and the [changelog](CHANGELOG.md).

Built by **Varun Pratap Bhardwaj** under **Qualixar**. This is an independent open-source integration, not an official TypeSafe, OpenAI, Anthropic, Google, Microsoft, or Laya product.

## What's new in 1.0.13

| Where | What changed |
|---|---|
| **Every host** | Choose **Jev**, **Laya** or **Jev + Laya** in setup, and set up local Laya with one command, [`jev laya-install`](#local-laya-optional-apple-silicon-mac). 55 recipes, 17 of them new for content creators, managers and developers. Faster hooks, decisions that run in parallel, and stronger consent and data screening |
| **Claude Code** | Session and subagent guidance names the exact arguments, including how to keep private content on your Mac under Jev + Laya, and says when an organization policy stops the plugin |
| **Claude desktop app** | Registration keeps your other servers and warns when the launcher sits in Documents, Desktop or Downloads. Every tool states its exact inputs |
| **Codex** | Session and subagent context names the exact workspace path and data scope. Hooks start through a launcher that finds Python 3.11 or later, and a prompt hook answers in about a second |
| **VS Code** | The `.vscode/mcp.json` entry uses `${userHome}`, so the file is safe to commit, and your key order and file permissions are kept |
| **Antigravity** | The first-step hint names the exact arguments and data scope |
| **Hermes** | Its tools state the same exact inputs as the MCP server |

The [changelog](CHANGELOG.md) has the summary.

## Who it is for

| You are | What Jev can do for you | A good first try | What stays yours |
|---|---|---|---|
| **Content creator** | Check a draft against a brief or a style rule, pick one of four formats, say which review a draft needs | The brief-fit recipe (`qualixar.brief-fit`) on a post that is already public | The words, the facts, and whether to publish |
| **Manager or team lead** (no coding) | Score one work item against priorities you write down, route an action to one of *your* teams, check a handoff or proposal against a requirement | The work-item-priority recipe (`qualixar.work-item-priority`) | Who does what, and when |
| **Developer** | Route a task, tool, skill or model tier from a list you define, check an extraction against its source, check whether retrieved passages answer a question, triage a diff | `jev_route` with your own candidates | The code, the tests and the merge |
| **Enterprise team** | The same decisions, with per-folder approval, daily limits, receipts, and a local-only option for client data | Read [Security and data handling](docs/SECURITY.md) first | Data-sharing approval, provider contracts, rollout |

Someone comfortable with a terminal has to do the one-time install (and, for the Claude desktop app, repeat one command after each upgrade). After that, everyone uses Jev by asking their assistant in plain words. [Working with Jev](docs/WORKING_WITH_JEV.md) has a first-session playbook for each of these people.

## Choose where decisions run: Jev, Laya, or Jev + Laya

The setup page asks you to choose one of three routes for each approved folder.

| Choice | Where each decision is made | What you need | Pick it when |
|---|---|---|---|
| **Jev** | Hosted, by TypeSafe or by OpenRouter, the one you choose. The text of each decision leaves your computer | An API key from that provider, which bills you under its own terms. You type the key into the private setup page, never into chat | The material may be shared with that provider |
| **Laya** | On your Mac. Nothing leaves the computer and there is no provider bill | An Apple-Silicon Mac with macOS 14 or later, and a one-time install with [`jev laya-install`](#local-laya-optional-apple-silicon-mac) (about 600–800 MB) | Client, confidential or personal material, or no hosted provider is allowed |
| **Jev + Laya** | Ordinary decisions go to Jev. A decision your assistant marks `restricted` (private or client content) is made by Laya on your Mac and never sent to Jev | Both of the above | A folder mixes shareable and private material |

Jev comes in three data levels in the setup page: **Jev public** (public text only), **Jev reviewed internal** (minimized internal text) and **Jev maximum** (reviewed workspace text, only if you are allowed to share it; it needs an extra confirmation). Together with **Laya only** and **Jev + Laya**, those are the five modes the setup page offers.

In Jev + Laya, your assistant decides per request which text is private, and the session guidance in Claude Code, Codex and Antigravity tells it how: pass `data_classification="restricted"` for private or client content. The secret screen does not recognize client names or contract terms. If a folder holds nothing but client material, choose **Laya only**. Jev-only modes never switch to Laya on their own, and Laya only never calls a hosted provider.

## Your first 10 minutes

1. **Check the two requirements** (1 minute): a Mac, and Python 3.11 or newer where the launchers look for it. See [Before you install](#before-you-install).
2. **Install for your host** (2–3 minutes). See [Install and upgrade](#install-and-upgrade). Quit and reopen the host afterwards.
3. **Try it offline** (1 minute). Ask your assistant: *"Run the Jev self-test."* (in Claude Code: `/jev-selftest`). It replays every shipped synthetic case through the real checking logic on your machine. No key, no network, no approval.
4. **Approve one folder** (3 minutes). Say: *"Set up Qualixar Jev for my Documents folder."* (in Claude Code: `/jev-setup ~/Documents`). A private page opens in your browser from a one-time link. If no browser window appears (for example over SSH), run `open-setup ~/Documents` from the plugin's `scripts` folder in a terminal (the same folder as `$JEV` below): a person at a terminal is shown the link to paste, and your assistant never is. Pick a route from the table above, paste a provider key there if you chose Jev, and confirm with your own click. Your assistant cannot approve this for you.
5. **Make one real decision** (2 minutes). *"Use Jev to decide whether this support request goes to billing or engineering: 'I was charged twice for one order.' Show the choice, the confidence and the receipt ID."*

A passing self-test shows that the checking logic works. It says nothing about how accurate the decision model is on your material.

## Before you install

| You need | Why | How to check |
|---|---|---|
| A Mac | This release is supported and verified on macOS. Linux is experimental, and Windows refuses to start | — |
| **Python 3.11 or newer** | The runtime is Python. The launchers look first in `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` and `/usr/bin/python3` (on macOS that one is 3.9 and **too old**), then at an absolute `JEV_PYTHON` you set, then for `python3.14` … `python3.11` and `python3` on your `PATH`, then `~/.pyenv/shims/python3` and `~/.local/bin/python3`. A Python inside the project folder the host opened is never chosen from `PATH`: point `JEV_PYTHON` at it if you want it | Run `python3 --version` in Terminal |
| For Jev: a TypeSafe or OpenRouter API key | Hosted decisions are billed by that provider | You enter it later, in the private setup page |
| For Laya: an Apple-Silicon Mac, macOS 14 or later, and `git` | Laya runs on the Mac's own chip | The setup page greys out Laya until [`jev laya-install`](#local-laya-optional-apple-silicon-mac) has verified an install |

If no Python 3.11 or newer is found, the tools never appear and the hooks stay silent. Any `jev` command in Terminal prints `PYTHON_3_11_REQUIRED`, lists every place it looked, and stops.

**Where it works:** Claude Code (terminal), the Claude desktop app (Chat and the Code tab, after the registration step below), Codex (desktop app and CLI), VS Code with Copilot agent mode, Antigravity and Hermes. **claude.ai in a web browser is not supported**: Jev runs as a program on your computer, and a browser session cannot start one.

## Install and upgrade

One section per host. Each gives the install step, the upgrade step, how to confirm, and the tool names your assistant sees. **After installing or upgrading on any host, quit that host completely and reopen it.** A running session keeps the version it started with.

### Claude Code

**Install**

```sh
claude plugin marketplace add qualixar/jev-decision-layer
claude plugin install qualixar-jev-decision-layer@qualixar
```

This adds seven commands (`/jev-setup`, `/jev-status`, `/jev-route`, `/jev-recipes`, `/jev-review`, `/jev-selftest`, `/jev-vscode`), three skills, the session hooks and the `qualixar-jev` MCP server.

**Upgrade.** `install` does not upgrade a plugin that is already installed: it reports that the plugin is already installed and changes nothing. Use `update`:

```sh
claude plugin marketplace update qualixar
claude plugin update qualixar-jev-decision-layer@qualixar
```

Then start a new session, or run `/reload-plugins` in the one you have open. A marketplace added from GitHub, like this one, does not auto-update by default. You can turn auto-update on under `/plugin` → **Marketplaces**. If you also registered Jev for the Claude desktop app, repeat [that step](#claude-desktop-app-chat-and-the-code-tab) after every upgrade.

**Confirm.** In a new session, run `/jev-status`. Before setup it says the folder is not enrolled, which is expected.

**Tool names:** `mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__<tool>`, for example `mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__jev_route`. Use this exact form in permission rules. To stop the prompts for the three tools that send nothing anywhere, add them to `permissions.allow` in your Claude Code `settings.json`:

```json
"mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__jev_auto_status",
"mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__jev_recipe_catalog",
"mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__jev_recipe_selftest"
```

Leave the tools that send text to a decision model on "ask" until you trust the folder's data scope.

**If your organization manages Claude Code**, its managed settings can stop the plugin's hooks, or keep the plugin from loading at all. `jev doctor`, `jev_auto_status` and the setup page say so when that is the case. [Enterprise-managed Claude Code](docs/HOSTS.md#enterprise-managed-claude-code) lists the settings an administrator can add, and a `CLAUDE.md` section that does the hook's job meanwhile.

### Claude desktop app (Chat and the Code tab)

In our tests the desktop app's Code tab did not start MCP servers that come from plugins: commands and skills load there, but the Jev tools do not. What the app does start, in Chat and in local Code tab sessions, is any server listed in its own desktop configuration. So you register Jev there, once, and again after each upgrade.

**Prerequisite:** the plugin is installed with the two Claude Code commands above. You register from that installed copy. The entry points at whichever copy ran the command, and a git clone moves, goes stale, or sits in a folder the app may not be allowed to run programs from. Register from a clone only when your organization's policy stops the plugin from installing, as [Enterprise-managed Claude Code](docs/HOSTS.md#enterprise-managed-claude-code) describes.

**Install**

1. **Quit the Claude desktop app completely** (Claude → Quit, or ⌘Q). The app keeps its configuration in memory and writes it back when it quits, so an edit made while it runs is lost.
2. In Terminal, run:

   ```sh
   JEV="$(ls -d "$HOME"/.claude/plugins/cache/qualixar/qualixar-jev-decision-layer/*/ | sort -V | tail -n 1)scripts/jev"
   "$JEV" host-register --host claude-desktop            # preview: shows the change, writes nothing
   "$JEV" host-register --host claude-desktop --write    # apply
   ```

   The first line picks the newest installed release. The preview shows the file it will change and lists your other servers under `preserved_servers`; they are kept. If you set `CLAUDE_CONFIG_DIR`, replace `"$HOME"/.claude` with that folder.
3. Reopen the app.

**Upgrade.** Upgrade the Claude Code plugin first. Then quit the desktop app and run the same three lines again. The entry for the older release is replaced. **If you skip this, Jev stops working in the app about two weeks after an upgrade**, because Claude Code deletes the previous version's folder 14 days after an update. An entry you added by hand, or one that carries your own `env` or `args`, is refused as `HOST_MCP_ENTRY_CONFLICT` and left alone. `host-register` warns when the launcher it registers sits in Documents, Desktop or Downloads, where macOS may not let the app run it.

**Confirm.** In a new chat, ask: *"Call jev_auto_status for my Documents folder."* Before setup the answer is that the folder is not enrolled, which is expected. Desktop Chat has no working folder, so name the approved folder in your request.

**Tool names:** `mcp__qualixar-jev__<tool>`. Plugin hooks do not run for a registered server, so add the [desktop `CLAUDE.md` section](docs/WORKING_WITH_JEV.md#claude-desktop-app-or-claude-mcp-add) if you want Claude to know the arguments without being told.

**No terminal at all?** Ask whoever manages your Mac to run step 2 once, and again after each upgrade. There is no terminal-free path in this release.

### Codex

**Install** (desktop app and CLI):

```sh
codex plugin marketplace add qualixar/jev-decision-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

**Upgrade.** Finish any running task and quit Codex Desktop first. A task that is still running can call a hook from the old version and fail with `can't open file .../hooks/jev_hook.py`. Then, from your system terminal:

```sh
codex plugin marketplace upgrade qualixar-jev-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

Reopen Codex and start a new task. [First use](docs/GETTING_STARTED.md#if-a-codex-hook-shows-an-error-after-an-upgrade) explains the Hook stats rows an upgrade can leave behind.

**Confirm.** `codex plugin list`, or the Plugins screen, shows the installed version. In a new task, ask for `jev_auto_status` on the project folder.

**Tool names:** the tools of the `qualixar-jev` MCP server, by tool name (`jev_route`). The Codex session and subagent hooks name the exact workspace path and `data_classification` the approval accepts; the [AGENTS.md section](docs/WORKING_WITH_JEV.md#codex-agentsmd) repeats them for sessions where hooks do not run.

### VS Code (Copilot agent mode)

VS Code has no hooks, so the whole integration is one `qualixar-jev` entry in the project's `.vscode/mcp.json`. In Claude Code, run `/jev-vscode` in the project. Or, in Terminal, with `$JEV` from the desktop step (or `plugins/qualixar-jev-decision-layer/scripts/jev` in a clone of this repository if you only use Codex):

```sh
"$JEV" vscode --workspace /absolute/path/to/project            # preview: writes nothing
"$JEV" vscode --workspace /absolute/path/to/project --write    # apply
```

Your other servers, key order and file permissions are kept, and the entry uses `${userHome}` rather than your home folder's path, so the file is safe to commit. A `.vscode/mcp.json` that does not parse is refused rather than overwritten; one with comments is refused as `HOST_MCP_CONFIG_HAS_COMMENTS`, and the command prints the entry for you to add by hand. Restart VS Code. **Upgrade:** run the same command again after each upgrade, because the entry points at a versioned folder. Add the [Copilot instructions section](docs/WORKING_WITH_JEV.md#vs-code-copilot-instructions) so Copilot knows the arguments. No live Copilot agent-mode turn has been run yet.

### Antigravity

Install the plugin folder `plugins/qualixar-jev-decision-layer` through Antigravity's own plugin install path. That provides the advisory `PreInvocation` hook and the skills. For the tools, quit Antigravity, then:

```sh
"$JEV" host-register --host antigravity            # preview
"$JEV" host-register --host antigravity --write    # apply
```

This adds one `qualixar-jev` entry to Antigravity's global MCP configuration and keeps the others. The first-step hint names the exact arguments the approval accepts. **Upgrade:** repeat both steps. Native tool turns in Antigravity have not been verified yet.

### Hermes

Install `plugins/qualixar-jev-decision-layer` through Hermes's own plugin install path, and repeat that after each upgrade. Hermes uses its own hook and tool entry points, and its tools state the same exact inputs as the MCP server. Native tool turns in Hermes have not been verified yet.

### From a local checkout

Replace `qualixar/jev-decision-layer` with the path of your clone in the `marketplace add` commands above. A clone also gives you the CLI at `plugins/qualixar-jev-decision-layer/scripts/jev`, for example the offline diagnosis:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev doctor --workspace /absolute/path/to/your/project
```

`jev doctor` is offline and makes no provider call. Before setup, `ACTION_REQUIRED` with `NOT_ENROLLED` and exit status `2` are expected. It does not prove that a host loaded the plugin. A marketplace install does not add a global `jev` command; use the full path, or `"$JEV"` from the desktop step.

### Local Laya (optional, Apple-Silicon Mac)

Laya modes stay greyed out in the setup page until a verified local install exists, and the page shows the exact command, with the right path, to create one. Run it in Terminal. With `$JEV` from the desktop step, that is:

```sh
"$JEV" laya-install
```

It checks for an Apple-Silicon Mac with macOS 14 or later, Python 3.11 or later and `git`, then builds Jev's own Python environment, installs the pinned Laya runtime from github.com and downloads the pinned model (about 600–800 MB) from huggingface.co. It hashes every model file against its pin and records the install only after the same check the setup page runs has passed. A download that does not match its pin is refused. Everything goes under `~/.local/state/qualixar-jev-decision-layer/` (or `$XDG_STATE_HOME/qualixar-jev-decision-layer/`).

| Option | Use it to |
|---|---|
| `--model multilingual` | Install the multilingual model instead of the English one (the default) |
| `--model-dir /absolute/path` | Use model files already on this computer instead of downloading them: a plain folder, or a Hugging Face cache snapshot of the pinned revision |
| `--python /absolute/path/to/python3` | Build the environment with a specific Python 3.11 or later |
| `--yes` | Skip the prompt. Without it, you type `INSTALL` to continue |

If your assistant runs the command for you, it has no terminal to type `INSTALL` into and must pass `--yes`, so read what the command downloads first. On a managed network, github.com and huggingface.co must be reachable. Then reopen the setup page and choose **Laya only** or **Jev + Laya**.

**What Laya can read.** Laya is a small model that reads at most 512 tokens per decision: the question, its options and your text together. That leaves room for roughly 250 to 300 words of your text. Longer input is refused with `MLX_STATE_WOULD_TRUNCATE` rather than silently cut, so a decision is never made on half the text. Pass the passage that matters, or use hosted Jev for long documents. Every shipped recipe is checked at build time to fit.

### Remove

See [Remove or roll back](docs/ROLLBACK.md). A desktop, VS Code or Antigravity registration survives uninstalling the plugin, so delete that `qualixar-jev` entry as well.

## After installing: approve one folder

Say: **"Set up Qualixar Jev for my Documents folder."** Or, in Claude Code, run `/jev-setup ~/Documents`. The `jev_setup` tool opens a private two-step page in your browser, on your own computer. You choose the route, see exactly what text may leave, set an expiry (1–365 days, 365 by default) and daily limits, enter a provider key if the route needs one, and tick the confirmation yourself. Your assistant cannot approve this for you, and installing the plugin authorizes nothing.

**One approval covers every host where the plugin is installed.** To cover every project under a parent folder, tick **child coverage**; the page pre-selects it for a folder that is not a Git repository. A folder you revoke stays off, with everything inside it, even under an approved parent, and revoking deletes the text Jev stored for it. A folder with its own narrower approval (for example Laya only) keeps that approval's rules for everything inside it. The explicit Jev tools (**Advisory Jev tools**) are on by default after setup; **automatic prompt guidance** is off until you turn it on.

## 60-second example

After setup, ask your agent:

> Route this synthetic duplicate-charge request between `billing` and `engineering`. Show the selected candidate, provider, model, route status, and receipt ID.

The prompt supplies a closed choice. A live result names the provider and model, the route status and a receipt; the selected candidate may vary. If none of the options fits, Jev answers `unknown` and your assistant decides the usual way. This route example does not return the recipe-specific `host_action`. The animation in [What can an agent actually do with it?](#what-can-an-agent-actually-do-with-it) illustrates the answer flow.

```text
Synthetic request + supplied candidates
                 ↓
        Typed Jev decision
                 ↓
       Local policy gate
                 ↓
    act / verify / ignore + receipt
                 ↓
      Host decides what to do
```

## Why an AI agent decision layer instead of a direct Jev call?

| A direct Jev call gives you | This layer adds |
|---|---|
| A typed answer from the selected provider | One shared runtime with host-specific adapters |
| A question you define for one call | 20 reusable legacy workflow contracts plus a catalog of 55 data-only recipe specifications; the 20 legacy workflows correspond to 20 of those 55 recipes, so these counts are not additive |
| Provider output | Workspace/provider scope, request limits, local `act / verify / ignore` gate, and a receipt |
| A hosted decision route | An optional local Laya route, installed with one command, for private decisions |
| Whatever validation you build around that call | 165 synthetic offline fixtures that exercise the shipped local gate, without a provider call |

TypeSafe's [published System One workflow comparisons](https://typesafe.ai/blog/introducing-system-one-models-and-jev) report Jev at **193.6× faster and 444.6× cheaper** than the LLM reference on those workflows, and its [model pricing](https://docs.typesafe.ai/models) lists **$0.042 per million input tokens**, with output tokens free. Those are the provider's figures for its workflows, not measurements of this plugin. The layer is useful when you need scope, limits, receipts and host integration as part of a repeatable workflow. It does not make a provider answer correct by itself, and it does not replace host permissions.

### Useful beyond the developer workflow

A manager can score one work item against priorities they write down. A content creator can ask which of four fixed formats suits a piece, or whether a draft meets a brief. A junior developer can ask for a bounded tool or skill suggestion. In every case the person supplies the context, reads the advisory answer and decides what happens next. They do this by asking their assistant in plain words; nobody writes JSON. The recipes with fixed options (formats, queues, categories) only choose among their own options; for your own list of teams or labels, ask the assistant to use Jev with your list, which calls `jev_route`. [Working with Jev](docs/WORKING_WITH_JEV.md) has the playbooks.

## What can an agent actually do with it?

![Illustrative three-step loop: ask a bounded question, receive typed probabilities, then let the agent verify before acting](docs/assets/decision-flow.gif)

*Illustrative flow, not a recording of a provider call. The numbers show the answer shape, not measured accuracy.*

| When you are working on… | What this layer can ask | What remains yours or the host's job |
|---|---|---|
| A coding task | Choose one task, tool, worker, or skill from candidates you supplied | Run the tool, edit code, test, and decide whether to accept the recommendation |
| A large file or search result | Select relevant context blocks and keep exact omitted text recoverable | Read the original source and verify any conclusion |
| A proposed patch | Score review attention and suggest the first area to inspect | Perform independent code review and run tests |
| A browser workflow | When several safe observed controls are plausible, rank a bounded choice; skip Jev for an obvious click | Grant browser access, execute the action, and inspect the resulting page |
| A freelance, creator, research, or support job | Try a typed recipe for brief fit, invoice exceptions, lead routing, citation checks, research ranking, and more | Supply evidence, handle uncertainty, and make consequential decisions |
| A failing step in an agent loop | Decide whether the failure is worth retrying, or whether retrying is waste | Enforce the retry budget, and run whatever is decided |
| A release | Check one requirement against the evidence you supply — version agreement, a changelog entry, test evidence | Inspect the repository, run the build, and decide to publish |
| A long tool result | Decide whether it contains anything that answers the goal, before the host reads it | Keep the original output, which stays authoritative |
| A structured extraction | Check every field against the source and return the probability each one is wrong | Decide what to do with a suspect field; re-extract or escalate |
| A set of retrieved passages | Score them absolutely and say whether they answer the question at all | Read the sources; abstain honestly when told to |

The package has **55 distinct data-only recipe specifications**. Its 20 original workflow contracts correspond to 20 of those recipes; they are not 20 additional recipes. This is a catalog of bounded use cases, not a claim that Jev is accurate on every user's data. Browse [everyday examples and recipe families](docs/USE_CASES.md), or ask the agent for `jev_recipe_catalog`.

### Prove it before you spend anything

Every recipe ships three synthetic cases — a clear-cut one, a genuinely ambiguous one, and one carrying an instruction hidden in material that is supposed to be data. Replaying all of them runs the real gate with no provider call, no key, and no workspace enrolment:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest
```

From an installed host, ask for the Jev self-test (`jev_recipe_selftest`, or `/jev-selftest` in Claude Code) instead. The three variants are enforced, not described: a clear-cut case must clear the raw gate, and an ambiguous or adversarial one must not. A fixture cannot be made to pass by recording whatever the gate happened to do. A passing run is a contract check on the shipped gate, never evidence of the provider's accuracy.

The TypeSafe question types are [Choice, Score, and Noul](https://docs.typesafe.ai/primitives): a selection from known options, a position on a defined scale, or a yes/no probability. The plugin wraps those answers in policy checks and receipts; it never treats a model answer as permission to act.

### The answer arrives already gated

For a live `jev_recipe_try`, the packaged gate evaluates the typed answer and returns a `host_action` plus a policy receipt ID. This describes a policy result; it does not run the recommendation or authorize execution.

| `host_action` | What it means | What the host should do |
|---|---|---|
| `act` | The answer passed the configured gate | No shipped recipe can retain `act` in this release: all are `SPECIFICATION_NOT_MODEL_EVALUATED`, so a would-be `act` is capped to `verify` |
| `verify` | Check the advisory answer independently | This is the maximum action for a would-be passing result from every shipped recipe in this release |
| `ignore` | The model selected `unknown` | Do not use a recommendation; decide normally |

Because every live recipe result is `verify` or `ignore` today, read `recommendation`, `confidence` and `reasons` to see what Jev actually said. The `jev_recipe_try` response is marked `EXPERIMENTAL_ADVISORY`. Its policy receipt records the recipe status, provider receipt ID, provider/model, and local gate result. A below-threshold or malformed answer remains `verify`; an explicit `unknown` may return `ignore`.

**Confidence is a summary of the distribution, not independent accuracy evidence.** [TypeSafe derives Choice and Score confidence from their reported probabilities](https://docs.typesafe.ai/confidence). The recipe gate applies both its configured confidence floor and probability bar as conservative policy settings; neither is calibrated on this repository's tasks. A Noul answer carries no confidence field, so its yes/no bands apply directly to its value.

**The gate fails closed.** A threshold that is missing, malformed, or out of range is a broken gate, not an absent one, and degrades to `verify` rather than `act`. So does a value outside its own domain, a label the model ranked below another, and an `act` that would carry no recommendation.

## How a decision moves through the system

![Architecture: agent to local policy broker, then hosted Jev or optional local Laya, then advisory answer and receipt back to the agent](docs/assets/architecture.svg)

The local broker checks the approved folder's scope, screens recognizable secrets, enforces daily request limits, and sends only the bounded state and questions needed for that call. Jev-only never silently falls back to Laya. Under Jev + Laya, a request marked `restricted` is decided by the verified local Laya install; the rest go to Jev. The answer returns to the agent as **advice plus an inspectable local receipt**; native tool and browser permissions remain unchanged. See [Security and data handling](docs/SECURITY.md).

An optional prompt hook can ask Jev for a relevant file or skill on *eligible* coding prompts. It is off until you turn it on in setup. It sends minimized prompt terms and candidate titles, not a whole repository. It is selective: the plugin cannot inspect every hidden choice inside a host or force the model to use a suggestion. It preserves original tool output and hands uncertain answers back to the agent.

### One runtime, per-host adapters

The shared runtime lives in `plugins/qualixar-jev-decision-layer/`. A host adapter is small on purpose, because **hook contracts differ in ways that matter for safety**:

| Host | Adapter | Why it differs |
|---|---|---|
| Codex | `jev_auto/hooks.py`, `hooks/codex-hooks.json`, `scripts/launch-codex-hook` | Hooks start through a launcher that finds Python 3.11 or later. Registers a `PostToolUse` hook on `Bash` and `mcp__filesystem__read_file` output. It acts only in folders that route text reduction to local Laya, and only on successful output |
| Antigravity | `jev_auto/agy_hook.py`, root `hooks.json` | Deliberately does **not** register PreToolUse: that contract requires a permission `decision` and can widen host trust |
| Hermes | `jev_auto/hermes_hook.py`, `jev_auto/hermes_tool.py` | Separate hook and tool entry points |
| Claude Code | `jev_auto/claude_hook.py`, `hooks/claude-hooks.json` | Uses `${CLAUDE_PLUGIN_ROOT}`, not `${PLUGIN_ROOT}`. Registers `SessionStart`, `UserPromptSubmit` and `SubagentStart` only; `PreToolUse` is not registered, so no hook can grant or deny a tool call |
| VS Code | `jev_auto/vscode_adapter.py` | No hook surface at all, so the adapter registers the same launcher as a workspace MCP server in `.vscode/mcp.json` |

Three of those hosts also take MCP registration, and they disagree about its shape in ways that fail silently: VS Code keys servers under `servers`, Antigravity and the Claude desktop config under `mcpServers`, and only VS Code expects a `type` field. `jev_auto/host_mcp.py` holds those shapes in one table with a test per row. Linux registration is experimental and unverified; Windows runtime support is disabled in this release.

Hook files are **per host and never merged** — the plugin-root variable differs, and Codex registers a matcher Claude Code deliberately does not. See `plugins/qualixar-jev-decision-layer/hooks/README.md`.

The secret screen is best-effort, **not comprehensive data-loss prevention**. Jev maximum requires an extra hosted-data confirmation, but that cannot grant permission to disclose somebody else's client or confidential information. Do not submit credentials or material you are not permitted to share. The provider key is entered only in the private setup page and stored in the macOS Keychain; do not paste it into chat, a repository file, or a shell argument.

## Provider thresholds and evidence

The shipped recipe floors are demonstration policy settings. The repository does not contain a reproducible, labeled provider evaluation sufficient to tune separate TypeSafe and Laya thresholds, so the local Laya route currently uses each recipe's unchanged floor. Its provider profile reports `UNVALIDATED_DEMONSTRATION_DEFAULT` with zero qualifying evaluation samples. The 165 offline fixtures check the local contract against hand-authored answers; they do not measure provider correctness, calibration, cost saved, or latency saved. Provider-specific thresholds require a labeled test set, a documented model revision, and held-out validation before they can be claimed.

## Supported hosts

Support is **not uniform**, and this table separates what has been observed from what has not. "Evidence" means something was run and produced the stated result. "Boundary" is what that evidence does *not* establish.

**Live end-to-end, every setup choice.** For 1.0.13, all 20 tools, all 55 recipes and every hook launcher were run through the shipped MCP launcher, started the way the Claude desktop app starts it, with real decisions: 83 of 83 checks passed with Jev, 83 of 83 with Laya, and 88 of 88 with Jev + Laya, including that restricted decisions stayed on the Mac. That is the shipped server and launchers, not a native tool turn inside each host.

| Host | Evidence | Boundary |
|---|---|---|
| **Codex** | Installed MCP tools and live synthetic TypeSafe routing verified on macOS; native Codex hook is a separate reviewed capability | Linux operation and automatic-hook coverage require separate host checks; Windows hosted runtime is disabled in this release |
| **Claude Code** | Plugin installs from the repo marketplace and `claude plugin validate` passes; seven commands and three skills load; the MCP launcher answers an `initialize` handshake; the hook launcher exits cleanly and stays silent on an unenrolled workspace | A native MCP tool turn inside a live session, and hook firing under a host that permits plugin hooks, are **not yet verified**. In the desktop app's **Code tab**, the plugin-provided MCP server did not load; register it as described in [Claude desktop app](#claude-desktop-app-chat-and-the-code-tab) |
| **Hermes** | Staged plugin doctor registers the declared tools and hook; installed copy awaits refresh | Native model/tool turn still needs verification |
| **Antigravity** | Packaged PreInvocation advisory hook and skills; this adapter does not request PreToolUse authority | Native model/tool turn and portable MCP registration still need verification |
| **VS Code** | Adapter writes a valid `.vscode/mcp.json` against the documented `servers` format; merge, refusal and symlink behaviour are covered by tests | **No live Copilot agent-mode turn has been run.** No extension ships; registration is the whole integration |

`jev laya-install` was run on an Apple-Silicon Mac, and the Laya and Jev + Laya runs above used that install. The Laya worker has also completed macOS sandbox file and network denial tests. Those results do not prove a particular GPU path or native host interception. The [capability manifest](docs/capabilities.json) and [agent-readable index](llms.txt) provide machine-readable pointers; the table above is the human-facing support boundary.

## Measure each decision

The broker reports provider calls and bytes, and the decision receipt lets you inspect what happened for a bounded choice. For task-level token, cost, and speed comparisons, use paired runs with the same host and an independently checked outcome. Recipe thresholds are configurable starting points; validate them against your own tasks before relying on an automatic action.

## Frequently asked questions

### What is Qualixar Jev Decision Layer?

An open-source MCP decision layer that lets an AI assistant hand small, closed choices to TypeSafe Jev or to local Laya, and get a typed answer with a receipt. It adds reusable recipes, per-folder approval, local policy gates and receipts around the model call. Codex, Claude Code, Hermes, Antigravity and VS Code reach the shared runtime through host-specific adapters.

### Can a manager or content creator use it without a terminal or JSON?

Mostly. Someone who can use a terminal does the one-time install, and for the Claude desktop app repeats one registration command after each upgrade. After that you use Jev by asking your assistant in plain words, for example *"Use the Jev work-item-priority recipe on this item against these priorities"*, and nobody writes JSON. The approval happens in a page in your browser. The optional [recipe workbench](docs/GETTING_STARTED.md#let-a-non-developer-explore-a-recipe), a local form for exploring recipes, is started from a terminal too.

### Does it work in claude.ai or the Claude mobile app?

No. Jev runs as a program on your own computer, and those surfaces cannot start one. Use Claude Code, the Claude desktop app after registration, Codex, VS Code, Antigravity or Hermes.

### Is it safe for client or confidential material?

Use **Laya only** for a folder of client or confidential material: nothing leaves the Mac. Hosted Jev sends the text of each decision to the provider you chose. The secret screen blocks recognizable credentials but not client names or contract terms. See [Security and data handling](docs/SECURITY.md) and its safe defaults for teams.

### Can Jev reduce AI agent token costs?

For a narrow choice that Jev handles directly, an agent can reserve its general-purpose model for generation and execution instead of using another chat-model answer for the decision. TypeSafe publishes [workflow speed and cost comparisons](https://typesafe.ai/blog/introducing-system-one-models-and-jev); the local receipt shows the decision used in your own workflow. This project has not measured savings in your workflow.

### Does this replace Codex, Claude Code, or the agent's main LLM?

No. Jev returns a typed recommendation for a bounded question. The host still controls file changes, shell commands, browser actions, approvals, and the final artifact. The local gate can return `act`, `verify`, or `ignore` as policy metadata; it does not grant execution permission.

## Build on it

The source is MIT-licensed. Add a data-only recipe with explicit input fields, a typed question, synthetic normal/uncertain/adversarial fixtures, and an honest limitation; see [CONTRIBUTING.md](CONTRIBUTING.md). Adding a host means writing one adapter against that host's hook contract and its own hook file — never editing another host's. The [rollback guide](docs/ROLLBACK.md) removes the plugin without deleting your project. Upstream code and model rights remain with their authors; see [third-party notices](plugins/qualixar-jev-decision-layer/THIRD_PARTY_NOTICES.md). No Jev or Laya model weights are included.
