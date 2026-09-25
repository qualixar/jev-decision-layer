<p align="center"><img src="docs/assets/jev-mark.svg" alt="Qualixar Jev Decision Layer mark" width="56" height="56"></p>

<h1 align="center">Qualixar Jev Decision Layer</h1>

<p align="center"><strong>Typed decisions for coding agents. Execution stays with the agent.</strong><br>
Route bounded task, tool, skill, and review choices through TypeSafe Jev, with optional local Laya.</p>

<p align="center"><a href="#install-in-codex-desktop">Get started</a> · <a href="docs/USE_CASES.md">Explore use cases</a> · <a href="https://github.com/qualixar/jev-decision-layer/releases/tag/v1.0.0">v1.0.0</a> · <a href="LICENSE">MIT license</a></p>

<p align="center"><img src="docs/assets/hero.svg" alt="Qualixar Jev Decision Layer: typed decisions for coding-agent workflows" width="820"></p>

**A decision layer for coding agents, not another chat model.** Give Jev a bounded question—*which tool, skill, task, file, or review path fits this state?*—and get a typed answer with probabilities and a local receipt. Your agent still writes code, uses tools, requests native permissions, and verifies the result. TypeSafe Jev is the primary decision model; [Laya-MLX](https://github.com/mizorewww/laya-mlx) is an optional, separately installed local route.

Built by **Varun Pratap Bhardwaj** under **Qualixar**. This is an independent open-source integration, not an official TypeSafe, OpenAI, OpenRouter, or Laya product. [Jev is TypeSafe AI's System One model](https://docs.typesafe.ai/introduction/coding-agents), designed to answer structured questions rather than generate prose.

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

The package has **20 original workflow contracts and 32 additional data-only recipe specifications**. That is a catalog of use cases, not a claim that Jev is accurate on every user's data. Browse [everyday examples and recipe families](docs/USE_CASES.md), or ask the agent for `jev_recipe_catalog`.

The TypeSafe question types are [Choice, Score, and Noul](https://docs.typesafe.ai/primitives): a selection from known options, a position on a defined scale, or a yes/no probability. The plugin wraps those answers in policy checks and receipts; it never treats a model answer as permission to act.

## How a decision moves through the system

![Architecture: agent to local policy broker, then hosted Jev or optional local Laya, then advisory answer and receipt back to the agent](docs/assets/architecture.svg)

The local broker checks the selected workspace scope, screens recognizable secrets, enforces daily request limits, and sends only the bounded state and questions needed for that call. Jev-only never silently falls back to Laya. In hybrid mode, the separately attested local Laya route can take selected private decisions. The answer returns to the agent as **advice plus an inspectable local receipt**; native tool and browser permissions remain unchanged. See the [security boundary](docs/SECURITY.md).

An optional Codex prompt hook can ask Jev for a relevant file or skill on *eligible* coding prompts. It sends minimized prompt terms and candidate titles, not a whole repository. It is selective: the plugin cannot inspect every hidden choice inside Codex or force the model to use a suggestion. It preserves original tool output and hands uncertain answers back to the agent.

For example, a freelancer can supply an enquiry and their own service categories, receive a suggested category and receipt, then decide whether to contact the lead. A creator can supply a brief and draft excerpt, receive an advisory fit judgment, then edit the draft. A developer can supply two plausible skills, receive a ranked suggestion or `unknown`, then choose what to load. A researcher can rank supplied evidence candidates but must still open the original sources.

## Five operating modes

The first-run wizard shows the exact folder, provider, text scope, expiry, and daily limits before you confirm. A globally installed plugin is available in every project, but **each workspace gets one reviewed scope**; installation alone does not authorize uploading future private repositories.

| Mode | Where a decision goes | Reviewed text scope |
|---|---|---|
| Jev public | TypeSafe or OpenRouter | Public text only |
| Jev reviewed internal | TypeSafe or OpenRouter | Minimized internal text |
| Jev maximum | TypeSafe or OpenRouter | Reviewed workspace text; may include client/confidential material only when you have authority to share it |
| Jev + Laya hybrid | Hosted Jev for reviewed public/internal decisions; attested local Laya for selected restricted decisions | Split by the reviewed policy |
| Laya-only | Local attested Laya; no Jev call | Local decisions |

The secret screen is best-effort, **not comprehensive data-loss prevention**. Jev maximum requires an extra hosted-data confirmation, but that cannot grant permission to disclose somebody else's client or confidential information. Do not submit credentials or material you are not permitted to share. The provider key is entered only in the private setup page and stored in macOS Keychain; do not paste it into chat, a repository file, or a shell argument.

## Install in Codex Desktop

**Platform for guided v1 setup:** macOS is required for the Keychain-backed hosted Jev wizard. Optional Laya-MLX additionally needs a supported Apple-Silicon macOS installation. This repository does not yet provide a tested Windows/Linux first-run credential flow.

From the public GitHub repository, add its local marketplace and install the plugin:

```sh
codex plugin marketplace add qualixar/jev-decision-layer
codex plugin add qualixar-jev-decision-layer@qualixar-jev-layer
```

Fully quit and reopen Codex Desktop so its skills, MCP tools, and hooks reload. Then say: **“Set up Qualixar Jev Decision Layer for this workspace.”** The `jev_setup` tool opens a private two-step browser wizard; ordinary users do not need a terminal command for the key or workspace enrollment. Explicit Jev tools are enabled by default after reviewed setup; automatic prompt guidance is **off until you turn it on** in that wizard. Review any native Codex hook-trust prompt separately. The [first-use guide](docs/GETTING_STARTED.md) shows how to verify the provider and receipt without exposing a key. A developer can install from a local checkout by replacing `qualixar/jev-decision-layer` above with `.`.

Once enrolled, try: **“Use Jev to route this synthetic duplicate-charge request between billing and engineering; show the selected candidate, provider, model, and receipt.”** A live result should name the chosen provider and a local receipt ID. A fixture result is simulated and does not verify provider access. The actual answer may vary; the important proof is the native tool and recorded provider call.

### Other agent hosts

| Host | Current evidence | Boundary |
|---|---|---|
| Codex Desktop | Installed MCP tools and live synthetic TypeSafe routing verified on this Mac | Automatic-hook coverage and savings are not proved by that call |
| Hermes | Staged plugin doctor registers 9 tools and 1 hook; installed copy awaits refresh | Native model/tool turn still needs verification |
| Antigravity | Packaged PreInvocation advisory hook and 2 skills; host adapter previously validated | No portable Jev MCP launcher or native model/tool turn verified in v1.0 |
| Claude Code | Portable plugin metadata is packaged | Experimental until tested in an installed Claude Code host |
| VS Code | No native VS Code extension is bundled in v1.0 | Not a verified v1.0 integration |

The optional Laya worker has completed local synthetic inference and macOS sandbox file/network-denial tests. Those results do not prove a particular GPU path or native Codex interception. The [capability manifest](docs/capabilities.json) and [agent-readable index](llms.txt) provide machine-readable pointers; the table above is the human-facing support boundary.

## About token, cost, and time savings

This release has **no measured Codex token, subscription-cost, API-cost, or task-time savings claim**. A Jev request has provider overhead and can make a task slower if it avoids no work. The broker reports actual calls and bytes; its saved-token and saved-cost fields remain unknown until matched, independently accepted host-task trials exist. Do not treat shorter text or fewer visible calls as proof of savings. Recipe and suggestion thresholds are uncalibrated demonstration defaults, not universal decision cutoffs.

## Build on it

The source is MIT-licensed. Add a data-only recipe with explicit input fields, a typed question, synthetic normal/uncertain/adversarial fixtures, and an honest limitation; see [CONTRIBUTING.md](CONTRIBUTING.md). The [rollback guide](docs/ROLLBACK.md) removes the plugin without deleting your project. Upstream code and model rights remain with their authors; see [third-party notices](plugins/qualixar-jev-decision-layer/THIRD_PARTY_NOTICES.md). No Jev or Laya model weights are included.
