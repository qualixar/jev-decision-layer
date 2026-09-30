# Security and data handling

This page is for users and for IT or security reviewers. It describes what the plugin does with data, as implemented in the code of this release: what leaves the machine and where it goes, what is screened, what is stored and for how long, how approvals are created and revoked, what an administrator can control, and a set of [safe defaults for teams](#safe-defaults-for-teams). Where a behaviour is best-effort, the page says so.

## What the product is, and is not

- A local plugin and MCP server that lets an AI assistant (Codex, Claude Code, the Claude desktop app, VS Code Copilot agent mode, Antigravity, Hermes) ask **bounded, typed questions**. The answer is a choice from a supplied list, a score on a supplied scale, or a yes/no probability.
- Answers are advice. No response grants or performs an action, and every response carries `"execution_authorized": false`. Every supported harness keeps its native permission prompts and Stop behavior.
- It is **not** a data-loss-prevention product, a sandbox for the assistant, or a guarantee of model accuracy. The product does not claim universal host interception, calibrated decisions on arbitrary domains, or measured token, cost or time savings.

## The permission boundary

The plugin asks a decision model bounded questions; it does not inherit authority to run commands, click a browser control, edit code, publish, or approve its own completion. A model answer is advisory until the host or a separate deterministic rule acts on it.

**The adapter surface is deliberately narrow.** Where a harness offers a hook contract that can widen trust, this package does not use it. Antigravity's `PreToolUse` requires returning a permission `decision`, so the Antigravity adapter registers `PreInvocation` instead and stays advisory. The Claude Code adapter registers only `SessionStart`, `UserPromptSubmit` and `SubagentStart`; `PreToolUse` is not registered, so no Jev hook can grant or deny a Claude Code tool call. The Codex adapter's `PostToolUse` hook acts only in folders that route text reduction to local Laya. The VS Code adapter registers a server and nothing else; it ships no extension and takes no editor permission.

**Source material is data, including when it contains instructions.** A recipe's typed question tells the model to treat embedded instructions in supplied material as data, and the shipped adversarial fixtures exercise exactly that: a passage asserting it is pre-approved, a commit message asking for approval without inspection, a candidate demanding to be ranked first. In every one the gate must refuse to authorise, and a test asserts it never returns `act`. `jev_recipe_selftest` replays all of them offline so you can confirm this yourself without a provider call.

## Components and where they run

| Component | Runs where | Network |
|---|---|---|
| MCP server (`launch-jev`) | A local process the host starts, under the user's account | None of its own. It talks to the local service |
| Local service ("broker") | A local process per approved folder, started on demand and reached over a private local socket. `jev stop --workspace <folder>` stops it | Makes the provider calls listed below, and only those |
| Host hooks (Claude Code, Codex, Antigravity, Hermes) | Short-lived local processes on host events | None of their own |
| Setup page and recipe workbench | Loopback pages on `127.0.0.1`, on a random port with a per-session token, opened in the user's browser | Loopback only |
| Optional local Laya | A separately installed, pinned model on an Apple-Silicon Mac, run in a macOS deny-by-default sandbox with no network in the tested configuration | None when deciding. `jev laya-install` downloads from github.com and huggingface.co once |

## Where a decision goes

The user chooses one mode per approved folder in the setup page.

| Mode | Processor | Text that may be sent |
|---|---|---|
| Jev public | TypeSafe **or** OpenRouter, the user's choice | Public text. Email addresses and home-folder paths are refused |
| Jev reviewed internal | Same | Minimized internal text. Emails and paths are allowed |
| Jev maximum | Same | Reviewed workspace text, including client or confidential text **only if the user has authority to share it**. Needs an extra confirmation in the setup page |
| Jev + Laya | The hosted provider for requests marked `public` or `internal-minimized`; local Laya for requests marked `restricted` | Split per request. A `restricted` request to `jev_route`, `jev_verify`, `jev_rerank`, `jev_review_diff` or `jev_recipe_try` (and `jev_typed_decide` with `provider: "laya-mlx"`) is decided on the Mac and never sent to the hosted provider |
| Laya only | Local Laya only | Nothing leaves the Mac. No hosted call is possible |

Hosted endpoints and models are fixed in code: `https://api.typesafe.ai/v1/systemone` (model `jev-1.13.0`) and `https://openrouter.ai/api/alpha/decisions` (model `typesafe/jev-1.13`). A request is an HTTPS `POST` with a body of `model`, `state` and `questions`, a bearer key, and the user agent `Qualixar-Jev-Decision-Layer/<version>`. A changed provider profile is refused (`PROVIDER_PROFILE_CHANGED`). A request never switches between hosted and local on its own: Jev-only modes do not fall back to Laya, and Laya only never calls a hosted provider. A failed hosted call is not retried, because the attempt may already be billed.

In Jev + Laya, the assistant chooses the `data_classification` of each request, and it cannot exceed what the approval allows. The session guidance in Claude Code, Codex and Antigravity for a Jev + Laya approval tells the model to mark private or client content `restricted`. That is a judgment the assistant makes about the text, not a detector: for a folder that holds only client material, **Laya only** is the safer choice.

## What each tool sends

| Tool or event | Sent to the processor | Consent required |
|---|---|---|
| `jev_route` | The task text (up to 4,000 characters) and candidate descriptions (up to 12 × 250) | An approved folder **and** the **Advisory Jev tools** option |
| `jev_verify` | Source text (up to 20,000 characters) and extracted field values (up to 32 × 2,000) | Same |
| `jev_rerank` | The query (up to 2,000 characters) and passages (up to 12 × 1,200) | Same |
| `jev_review_diff` | The goal (up to 1,000 characters) and diff (up to 16,000) | Same |
| `jev_recipe_try` | The recipe's input fields and question | Same |
| `jev_typed_decide` | Caller-supplied state and questions (up to 60 questions) | Same |
| `jev_reduce` | The goal and the supplied text, split into blocks (at most 48). In hosted modes these go to the hosted provider | An approved folder only. **The Advisory Jev tools option is not required** |
| `jev_prepare`, explicit call | Nothing, unless automatic prompt guidance is on. Otherwise it builds a local shortlist from file names | An approved folder |
| Automatic prompt guidance (off by default) | Up to 24 prompt words and up to 12 file or skill *names*, never file contents | A separate opt-in in the setup page, on top of Advisory Jev tools |
| Codex tool-output reduction (`PostToolUse`) | Only to local Laya, never to a hosted provider | A folder whose text reduction is routed to Laya |
| `jev_recipe_selftest`, `jev_recipe_catalog`, `jev selftest`, `jev doctor` | Nothing. Offline | None |

Terms and file names can still disclose project information. Use automatic prompt guidance for restricted work only when the chosen provider scope and your review permit that transfer. A provider failure leaves the host in control and is never replaced with a simulated answer.

## What is screened before anything leaves

Every hosted request, and every response, is checked against one fixed set of patterns, shared by every tool. A match blocks the whole request (`SENSITIVE_PAYLOAD_NOT_SENT` or `INPUT_DATA_BLOCKED`) and nothing is sent:

- private keys of any type (RSA, EC, DSA, OpenSSH, encrypted PKCS#8, PGP, PuTTY);
- a named credential given a value, in code, JSON, YAML, INI, TOML or `.env` form: `password`, `passwd`, `secret`, `api_key`, `access_key`, `private_key`, `client_secret`, `auth_token`, `access_token` and similar names, including prefixed ones such as `DB_PASSWORD` or `aws_secret_access_key`, and a one-word password on its own line (`password: meadowlark`). Code that only reads a credential (`password = input()`, an environment lookup), schema words (`password: required`) and obvious placeholders (`${DB_PASSWORD}`, `<your-key>`, `changeme`, `user:password`) are not blocked. In a JSON or translation file, a single dictionary word such as `"password": "Passwort"` is treated as a label;
- `Authorization: Basic …` and `Bearer …` values, `curl -u user:secret`, a password inside a URL (including `redis://:secret@host`), and a container registry `auth` field;
- common token formats: OpenAI and Anthropic (`sk-…`), GitHub (`gh…_`, `github_pat_…`), GitLab (`glpat-…` and the other GitLab token types), AWS and Amazon Bedrock keys, Slack tokens and webhooks, Microsoft Teams webhooks, Google API keys and OAuth tokens, Stripe, npm, PyPI, Hugging Face, SendGrid, Shopify, Linear, Notion, Postman, PlanetScale, Grafana, Heroku, HashiCorp Vault and Terraform, DigitalOcean, Databricks, Docker Hub, Figma, Sentry, Twilio and about thirty more documented prefixes, JWTs, Azure AD client secrets, and Azure `AccountKey=`, `SharedAccessKey=` and SAS signatures;
- URLs on `.internal`, `.local`, `localhost` or loopback addresses, and private IPv4 ranges;
- any object key named like `password`, `secret`, `api_key`, `credential`, `private_key` or `token` that has a value, except the option labels of a choice question (a triage label named `password` is a category);
- the active provider key itself;
- **in public scope only:** email addresses and home-folder paths.

Text is checked as written, with invisible characters removed and look-alike characters normalised (so a zero-width space cannot split a key), and once more URL-decoded.

**Limits.** This is a pattern screen, not DLP. It does not recognize names, client identifiers, contract terms, confidential source code, or secrets in unusual formats. The data scope is chosen by the user in setup; within that scope, the assistant decides what text to include.

## Credentials

- Hosted keys are typed by the user into the loopback setup page, never into chat or a tool argument. They are stored in the **macOS Keychain** under the service `ai.qualixar.adl.typesafe` or `ai.qualixar.adl.openrouter`.
- Folders approved from the terminal with `jev enroll`, a developer path, can read the key from the `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` environment variable instead.
- Keys are never written to receipts, logs or model-facing output. Error messages are fixed codes.

## What is stored locally, and for how long

All state is per user, under `$XDG_STATE_HOME/qualixar-jev-decision-layer/` (by default `~/.local/state/qualixar-jev-decision-layer/`), in owner-only directories and files, with one sub-folder per approved folder. Private state protects the Jev leaf directory and files on the supported macOS runtime. Code already running as the same user can read or change it; that is outside the product's tamper-resistance claim.

| Item | Contents | Kept for |
|---|---|---|
| `policy.json` | The approval: mode, provider, data scope, limits, expiry, options | Until it expires or is revoked. A revoked approval stays as a record so that a parent approval cannot reopen the folder |
| Decision receipts | Digests of the request and evidence, provider, model and the typed answers. For the typed tools the input text itself is **not** stored | `retention_days`: 7 by default, 30 at most, pruned hourly while the local service runs. **Deleted at once when you revoke the folder.** An expired folder's receipts are aged out by its own retention whenever any Jev service runs |
| Reduction records (`jev_reduce`) | The **full original text**, for exact recall, plus digests. The context tool keeps omitted text in this private receipt; do not treat a reduced view as a new source of truth | Same retention |
| Legacy workflow records (`jev_evaluate`) | The submitted state and questions | Same retention |
| Codex prompt goals | The session's prompt text (up to 4,000 characters), after the screen | Same retention; ignored after 24 hours |
| Answer cache | Typed answers, keyed by request digest | 10 minutes by default, 1 hour at most |
| Daily counters | Date, call count and byte count per approval | Not pruned. Numbers only |
| Local Laya install | Jev's own Python environment, the pinned model files, their hash manifest and the install record (`mlx-env/`, `laya-models/`, `mlx-installation.json`) | Until you delete them |

Removing the plugin does **not** delete this folder or the Keychain items (see [Remove or roll back](ROLLBACK.md)).

## Approvals: created, expired, revoked

- **Created only by a person.** The assistant can *open* the setup page (`jev_setup`), but never receives its address: the page opens from a one-time 256-bit link handed to the browser alone, and a copied or replayed link is refused. A person at a terminal running `open-setup` is shown the link to paste if no browser opens. Another program that finds the port gets no session. The link can briefly appear in the browser opener's process arguments, and a same-user program that reads browser history is outside this boundary. The approval needs the user to review the scope and tick a confirmation. Jev maximum needs a second confirmation, and covering sub-folders ("child coverage") another. The terminal path (`jev enroll`) needs an interactive terminal and typed confirmations (`ENABLE`, then `COVER CHILDREN`); a non-interactive run is refused with `PRIVATE_TERMINAL_SETUP_REQUIRED`.
- **Scope.** One approval per folder or Git repository root. A descendant grant is a separate consent from the workspace itself: it is off unless the setup review or `jev enroll --cover-descendants` records it, and it can never be attached to a home folder or a filesystem top such as `/`, `/Users` or `/tmp`. A child with its own policy or a local refusal is outside that grant, and so is everything inside that child: a folder with its own narrower approval (for example Laya only) is never overridden inside by a broader parent. Its subfolders are covered only if its own approval covers them. Hosted calls still follow the grant's data scope.
- **Expiry.** 1–365 days, 365 by default. After expiry, calls fail with `AUTO_DISABLED_OR_EXPIRED`, and Claude Code sessions are told once that no approval is active. An expired child approval falls back to an approved parent.
- **Revocation.** `jev revoke --workspace <folder>` takes effect at once: it waits only for decisions already in flight, no new one starts, and a request for that folder that was already queued is refused before any provider call. A revoked or refused folder stays off for everything inside it, even when a parent folder is approved, and revoking deletes the receipts, cached answers and prompt goals Jev stored for it. On a Mac the folder may be typed in any letter case. Refusing a folder does not stop an assistant working in an approved parent from reading files in it and passing their text as content; keep such folders out of approved parents. A folder no approval covers cannot be refused in advance: revoke then returns `WORKSPACE_NOT_ENROLLED` and records nothing.
- **Changes.** Reopening setup shows the current and proposed scope side by side. A concurrent change is detected and refused (`POLICY_CHANGED`).
- **Local Laya is off until chosen.** `local_laya_enabled` ships false. Laya modes can be chosen only after `jev laya-install` has verified a pinned install, and enabling Laya against an unverified install is refused with `LOCAL_ROUTE_NOT_ATTESTED`. Installing this package does not activate a local model for anyone.

## Limits

Defaults per approval, adjustable in setup within the bounds shown: **1,000** calls per day (1–100,000), **20 MB** per day, **48 KB** per request (1–100 KB), and a **12 s** timeout (1–30 s). A request that reached the provider counts against the limit even if it then failed.

## Local Laya, for restricted or client data

Laya is the route for material that must not leave the Mac. `jev laya-install` installs the Laya runtime from github.com at a pinned commit, into Jev's own Python environment, and the model from huggingface.co at a pinned revision, or copies it from a folder or a Hugging Face cache snapshot of that revision. It hashes every model file, refuses a weights file that does not match its pin, and records the install only after the same verification the setup page runs. The process uses a macOS deny-by-default sandbox and a no-network inference path in the tested source bundle. That does not establish protection against hostile code already running as the same user, nor prove a particular GPU execution path. Model weights are not distributed in this repository.

## What an administrator can control

| Goal | Control |
|---|---|
| Block the Claude Code plugin, its commands, hooks and tools | Claude Code managed settings: leave the marketplace out of `strictKnownMarketplaces`, or list it in `blockedMarketplaces` |
| Allow it, including its hooks, where only managed hooks may run | Managed `extraKnownMarketplaces` and `enabledPlugins: {"qualixar-jev-decision-layer@qualixar": true}`. The exact JSON is in [Enterprise-managed Claude Code](HOSTS.md#enterprise-managed-claude-code) |
| Block or allow only the tools | `allowedMcpServers` / `deniedMcpServers`, as described in the same section |
| Prevent any hosted processing | Block egress to `api.typesafe.ai` and `openrouter.ai` at the proxy. Hosted calls then fail and nothing falls back. Laya only keeps working |
| Allow hosted processing through one processor only | Allow only that host at the proxy |
| Remove stored keys | Delete the Keychain items named under [Credentials](#credentials) |
| Audit use on a machine | `jev stats --workspace <folder>` (counters), `jev doctor --workspace <folder>` (offline check), receipts through `jev recall` |

**Not available in this release:** a central, administrator-enforced Jev policy, for example "Laya only for everyone", "at most 30 days" or "no Jev maximum". Mode, expiry and limits are chosen per user in the setup page. To enforce them, use the network and host controls above.

## Safe defaults for teams

A one-page rule set for rolling Jev out to a team.

**1. One folder per sensitivity level, never one approval for everything.**

| Folder holds | Choose | Why |
|---|---|---|
| Public material: published posts, open-source code, synthetic examples | **Jev public** | Only public text may leave. Emails and home paths are refused |
| Internal, non-client material: plans, internal docs, your own code | **Jev reviewed internal** | Minimized internal text goes to the hosted provider you chose |
| **Client, customer, personal or contract material** | **Laya only**, on an Apple-Silicon Mac after `jev laya-install`. Otherwise **do not approve the folder** | Nothing leaves the machine. The secret screen does not recognize client names or contract terms |
| Mixed material where only some decisions are sensitive | **Jev + Laya**, only if the team understands that the assistant marks each request | Only requests marked `restricted` stay local |

Do not use **Jev maximum** for client material unless the data owner has agreed that it may go to that provider. Your own checkbox in setup is not the data owner's permission. Do not approve a parent folder with child coverage if client projects sit inside it; approve the other folders one by one. If you already approved the parent, revoke each client sub-folder at once with `jev revoke --workspace <sub-folder>`: that records a refusal, and the sub-folder stays off under the approved parent.

**2. Settings in the setup page.**

| Setting | Team default | Reason |
|---|---|---|
| Advisory Jev tools | On for developers; on for others after their first session | Without it, only file shortlists and text reduction work |
| Automatic prompt guidance | **Off** | It sends prompt words on eligible prompts and uses the daily budget. Turn it on only after checking that it helps |
| Approval length | **90 days** for internal folders, **30 days** for anything sensitive, rather than the 365-day default | Short approvals get reviewed again |
| Daily calls and bytes | The defaults (1,000 calls, 20 MB), or lower for a pilot | Limits bound cost and exposure |
| Provider | One processor per team, recorded in your supplier register | Two processors means two sets of terms to review |

**3. Host settings.** For Claude Code, allow the marketplace and force-enable the plugin in managed settings if hooks must run under `allowManagedHooksOnly`. Let `permissions.allow` auto-approve only the offline tools (`jev_auto_status`, `jev_recipe_catalog`, `jev_recipe_selftest`), and keep the text-sending tools on "ask" during a pilot. To keep hosted processing off for a group, block the two provider hosts at the proxy.

**4. Working rules for everyone.**

1. Jev answers are advice. Nobody skips a review, a test or an approval because Jev agreed.
2. Never paste keys, passwords or client identifiers into a prompt that may reach Jev.
3. `unknown` means "decide yourself". Do not rephrase until you get the answer you wanted.
4. Send the excerpt that matters, not whole documents.
5. After every plugin upgrade, re-register Jev for the Claude desktop app, VS Code and Antigravity (see the [README](../README.md#install-and-upgrade)).

**5. Before calling a pilot a success.** A passing offline self-test only proves the checking logic is intact. Judge Jev on your own tasks: a small labeled set, the same prompts with and without Jev, and an independent check of the outcome. The project makes no measured savings claim, and neither should a rollout note.

## Known boundaries

- Release 1.0.13 is supported and verified on macOS, with the Keychain for hosted keys. Linux remains experimental and unverified. Windows implementation code remains in the tree but is unreachable through the broker entry point, because its native private-state contract did not pass CI.
- The provider's own data handling is governed by that provider's terms, not by this project. Review them before choosing a hosted mode for internal text.
- Thresholds are demonstration defaults. No recipe has been evaluated against labeled provider answers.
- Writing into a user's files is bounded and reversible. Host registration merges one `qualixar-jev` entry into a host configuration rather than replacing it, refuses a file it cannot parse instead of discarding settings it cannot read, never follows a symlink, and defaults to a preview that writes nothing.
- Browser choice is limited to observed controls in an already authorized tab. A page can change between observation and action, so the bridge checks freshness and returns uncertain or consequential actions to the host. `DONE` still requires independent inspection. The automatic completion collector is disabled.

## Checks a reviewer can run without a key

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest                              # every shipped synthetic case, offline
plugins/qualixar-jev-decision-layer/scripts/jev doctor --workspace /absolute/folder   # offline diagnosis
plugins/qualixar-jev-decision-layer/scripts/jev host-register --host claude-desktop   # prints the planned change, writes nothing
```

## Reporting a vulnerability

Report a suspected vulnerability without including live credentials or private user material in a public issue.
