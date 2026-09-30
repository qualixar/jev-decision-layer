# Working with Jev

Jev answers one kind of question well: **a bounded choice over material you supply.** Pick one of these options. Place this on that scale. Is this true, yes or no. It answers in one typed call, and it does not write, fetch, run or approve anything. Use it when a wrong small choice would cost you a detour. Skip it when the answer is obvious, or when you cannot write the options down.

This page is for you and for your assistant (the "host model"). It covers when to call which tool, how to phrase a request, a first-session playbook for each kind of user, the exact argument shapes, how to read an answer, and [instruction templates](#instruction-templates-for-claudemd-agentsmd-and-copilot) you can paste into `CLAUDE.md`, `AGENTS.md` or Copilot's instructions. Install and setup are in the [README](../README.md#install-and-upgrade); security and data handling are in [SECURITY.md](SECURITY.md).

## When to call which tool

| The situation | Tool | Branch on | Do not use it when |
|---|---|---|---|
| Choose one of 2–12 options you can list: a task path, tool, skill, team, format or model tier | `jev_route` | `candidate_id`. When `status` is `ABSTAIN_UNKNOWN`, `candidate_id` is `null`: decide yourself | One option is plainly right, or you cannot list the options |
| A structured extraction (fields pulled from an invoice, email or document) must match its source | `jev_verify` | `trustworthy`. **Never** branch on an empty `suspect_fields` list: a field in `unknown_fields` was not judged, which is not the same as clean | You have no source text to check against |
| Retrieved passages or memories must actually answer a question before you rely on them | `jev_rerank` | `should_abstain`. If it is true, say you do not have the answer | You only need a relative order: your search score already gives you that |
| A diff is ready and you want to know where to look first | `jev_review_diff` | `focus` and `risk`, as where to start reading and nothing more | As a substitute for tests or review. It never approves |
| A shipped recipe matches your question exactly (see the [playbooks](#playbooks) and [use cases](USE_CASES.md)) | `jev_recipe_try` | `recommendation`, `confidence` and `reasons`. In this release `host_action` is always `verify` or `ignore` | The recipe's fixed options do not match your own categories. Use `jev_route` with your own list |
| Any other bounded question: several yes/no checks, or a scale with your own labels | `jev_typed_decide` | Each answer's `choice`, `score` or `noul`, plus its `confidence` | Open-ended writing, summarising or research |
| Which files matter for a narrow task | `jev_prepare` | The shortlist | Broad exploration |
| A long text where you want only the parts relevant to a goal, with the rest recoverable | `jev_reduce`, then `jev_recall` | The reduced view. The original stays authoritative | Text with secrets. It is returned unchanged and nothing is sent |
| Is Jev set up here, and how much has been used today? | `jev_auto_status` | Enrollment, provider, counters | — |
| Check the checking logic, offline and free | `jev_recipe_selftest` | `all_passed` | — |

`jev_route`, `jev_verify`, `jev_rerank`, `jev_review_diff`, `jev_recipe_try` and `jev_typed_decide` need the **Advisory Jev tools** option in your approval. Without it they return `GENERIC_QUERY_NOT_ENROLLED`.

## How to phrase a request

Your assistant turns a plain request into a tool call. Name the options and the material.

- "Use Jev to pick the best format for this post: short video, newsletter, carousel or short post. Here are the summary, audience and goal: …"
- "Ask Jev whether this invoice extraction matches the PDF text below. Tell me which fields are suspect or unknown."
- "Before you answer from these search results, have Jev check whether they actually answer my question."
- "Route this task with Jev between `investigate`, `implement` and `review`. Show the confidence and the receipt ID."
- "Run the Jev self-test for the brief-fit recipe, the uncertain case."

Avoid "use Jev to do X". Jev does not do things. Ask it to choose, score or check.

## Playbooks

Every recipe named here ships in this release. The field names are the exact inputs the recipe accepts today; `jev_recipe_catalog` always shows the current fields and options. No recipe has been evaluated against labeled answers yet, so every live recipe result comes back marked for you to check (`verify`).

### Content creator

**What to expect.** Jev does not write, rewrite, fact-check or publish. It gives a fast, consistent second opinion on one question at a time: does this draft meet the brief, which of four formats suits this piece, which review does it need before it goes out. You decide.

**Your first session** (after someone has installed it):

1. Ask: *"Show me the Jev recipes for content creators and explain each in one sentence."* This lists the catalog. Nothing is sent anywhere.
2. *"Run the Jev self-test for the brief-fit recipe, all three cases, and tell me in plain words what each one shows."* Offline and free. You see a clear case, an ambiguous case, and a case with a hidden instruction; Jev stays careful on the last two.
3. *"Set up Jev for my Documents folder."* A private page opens. Choose **Jev public**, leave **Advisory Jev tools** ticked, paste your provider key into the page itself, and confirm.
4. With a post that is already public: *"Use the Jev brief-fit recipe. The brief is: … The draft is: … Tell me the score, the confidence and why."*

| You want to know | Recipe | Fields | Notes |
|---|---|---|---|
| Does the draft meet the brief? | `qualixar.brief-fit` | `query` = the brief, `candidate` = the draft excerpt | A three-level score: does not, partly, or directly meets it |
| Does it follow the style guide? | `qualixar.brand-tone` | `query` = one style rule, `candidate` = the excerpt | One rule at a time. Give it the rule, not a mood |
| Which format suits it? | `qualixar.content-repurpose` | `source_asset_summary`, `audience`, `goal`, `production_constraints`, `rights_review_status` | Fixed options: `short_video`, `newsletter`, `carousel`, `short_post`. Unclear rights give `unknown` |
| Which review does it need before publishing? | `qualixar.publication-review` | `draft`, `checklist` | Fixed options: `editor_review`, `evidence_review`, `privacy_review`, `disclosure_review` |
| Does my source back this claim? | `qualixar.claim-verification` | `claim`, `evidence` | A yes/no likelihood. Still open the source yourself |

**Your own formats or labels?** Skip the recipe: *"Use Jev to choose between reel, long-form article and podcast clip for this piece: …"* Your assistant calls `jev_route` with your options.

**Do not** put client or embargoed material in a **Jev public** folder. Keep it in a separate folder approved for **Laya only**.

### Manager or team lead

**What to expect.** A consistent, explainable score or routing suggestion for one item against rules *you* write down. Jev does not see your roadmap, your people or your tools, and it never assigns or sends anything.

**Your first session:**

1. *(one time, may need IT or a colleague)* Install, and for the Claude desktop app, register it: [README → Claude desktop app](../README.md#claude-desktop-app-chat-and-the-code-tab). This needs Terminal once, and again after each upgrade.
2. In a new chat: *"Call jev_auto_status for my Documents folder."* "Not enrolled" is expected before setup.
3. *"Set up Jev for my Documents folder."* For internal notes choose **Jev reviewed internal**. For client material choose **Laya only** (Apple-Silicon Mac, after `jev laya-install`), or keep that material out of Jev.
4. *"Use the Jev work-item-priority recipe. Work item: … Our priorities: 1) … 2) … Known constraints: … Decision horizon: this quarter. Give me the score and the reasons."*

| You want | Recipe or tool | Fields |
|---|---|---|
| A consistent read on one work item against written priorities | `qualixar.work-item-priority` | `work_item`, `stated_priorities`, `known_constraints`, `decision_horizon` |
| Is a handoff really ready? | `qualixar.handoff-readiness` | `requirement`, `artifact_evidence` |
| Does a proposal cover a requirement? | `qualixar.proposal-requirements` | `requirement`, `proposal_excerpt` |
| Does the release evidence meet one stated requirement? | `qualixar.release-readiness` | `requirement`, `release_evidence` |
| Which of **my** teams owns this action? | `jev_route` with your own list | *"Use Jev to route this action between Finance, Legal and Delivery: …"* |

**Why not the meeting-action or lead-routing recipes for your own teams?** Their options are fixed. The meeting-action recipe only chooses among `content`, `customer_support`, `engineering` and `operations`; the lead-routing recipe only among `consulting`, `general`, `partnership` and `product_help`. Your description of your teams is context for that choice, not a new list of options. If your teams have other names, use `jev_route` with your own list.

**Good habits.** Score one item per call and compare the reasons. Write the priorities down once and reuse the same text: the same input gives comparable output. Never paste personal data about a named person. Jev scores one item; it cannot rank a backlog.

### Developer

**What to expect.** A cheap, typed side call for closed choices and checks, with a local receipt. It costs one round-trip, so use it where a wrong choice costs more than that.

**Your first session:**

1. Install, then run `/jev-selftest` (Claude Code) or ask for the Jev self-test. Every case should pass, offline.
2. `/jev-setup ~/code` with child coverage if your repositories live under one parent folder. Choose **Jev reviewed internal** and tick **Advisory Jev tools**. Leave **automatic prompt guidance** off until you have seen what it adds.
3. Allow the three offline tools in `permissions.allow` (names in the [README](../README.md#claude-code)).
4. *"Route this with Jev: tasks `investigate`, `implement`, `review`. Task: …"* Check `candidate_id`, `confidence` and `receipt_id`.

| Moment | Call | Branch on |
|---|---|---|
| Choosing a model tier, subagent or workflow | `jev_route` with `kind: "task"` and your own ids (`sonnet`, `opus`…) | `candidate_id`; `null` means your call |
| Before trusting an LLM extraction (config values, parsed logs, invoice fields) | `jev_verify` | `trustworthy`, then `unknown_fields` |
| Before answering from RAG or memory hits | `jev_rerank` with `[{"fact_id", "content", "score"}]` | `should_abstain` |
| A long diff | `jev_review_diff` | Where to start reading |
| A tool failed | recipe `qualixar.retry-decision` (`failure`, `attempts_so_far`, `available_actions`) | Stop retrying on `stop` or `escalate`. The options are fixed: `retry_unchanged`, `retry_with_change`, `stop`, `escalate` |
| A long tool output | recipe `qualixar.output-relevance` (`goal`, `tool_output`) or `jev_reduce` | Whether it is relevant, not whether it is correct |
| Did I really finish? | recipe `qualixar.completion-gate` (`criterion`, `implementation_evidence`) | It is evidence for you, not a pass |

**Keep the overhead down.** Keep automatic prompt guidance off unless it helps, add an instruction template only where hooks do not run, allow the offline tools, and do not call Jev for obvious choices.

### Enterprise team

Start with [Security and data handling](SECURITY.md): what each mode sends to which processor, what is screened, stored and retained, and the [safe defaults for teams](SECURITY.md#safe-defaults-for-teams). The short version: one folder per sensitivity level, **Laya only** for client or personal data, short approvals, automatic prompt guidance off, and managed Claude Code settings from [Enterprise-managed Claude Code](HOSTS.md#enterprise-managed-claude-code).

## Exact argument shapes

Every workspace tool needs `workspace_path`, an **absolute** folder path covered by an approval. The tools that send text also need `data_classification` ([see below](#which-data-classification-to-pass)). Leaving either out returns `MCP_ARGUMENTS`, which does not say which field is missing.

```jsonc
// jev_route: every candidate has exactly these two keys
{"workspace_path": "/abs/path", "kind": "task",            // "task" | "tool" | "skill"; use "task" for model tiers
 "task": "what is being decided, up to 4000 characters",
 "candidates": [{"id": "sonnet", "description": "Default tier for described edits"},
                {"id": "opus",   "description": "Hard reasoning and security review"}],
 "data_classification": "internal-minimized"}
// ids: a letter first, then letters, digits, _ or -, up to 64 characters. No dots or colons. "unknown" is reserved.
// description: 1-250 characters. 2-12 candidates. Bare strings, or any extra key such as "label": ROUTE_CANDIDATES_INVALID.

// jev_verify: one probability-of-being-wrong per top-level field, up to 32 fields
{"workspace_path": "/abs/path", "source_text": "up to 20000 characters",
 "extraction": {"invoice_number": "42", "total": "10.00 EUR"},
 "threshold": 0.7,                                          // optional, default 0.7
 "data_classification": "internal-minimized"}

// jev_rerank: each memory needs "content"; "fact_id" and "score" are optional and echoed back
{"workspace_path": "/abs/path", "query": "up to 2000 characters",
 "memories": [{"fact_id": "m1", "content": "passage text", "score": 0.82}],   // 1-12; content is cut at 1200 characters
 "data_classification": "internal-minimized"}
// {"text": ...} instead of {"content": ...}: RERANK_MEMORIES_INVALID.

// jev_review_diff: a unified diff with its --- and +++ lines, as git diff prints it
{"workspace_path": "/abs/path", "goal": "up to 1000 characters", "diff": "up to 16000 characters",
 "data_classification": "internal-minimized"}

// jev_recipe_try: "input" holds exactly the recipe's fields (see jev_recipe_catalog)
{"workspace_path": "/abs/path", "recipe_id": "qualixar.brief-fit",
 "input": {"query": "the brief", "candidate": "the draft excerpt"},
 "data_classification": "public"}
// A missing or extra field: RECIPE_INPUT_INVALID.

// jev_typed_decide: up to 60 questions, at most 20000 bytes of questions
{"workspace_path": "/abs/path", "provider": "typesafe", "data_classification": "internal-minimized",
 "state": {"draft": "..."},
 "questions": {
   "tone":  {"type": "score",  "instructions": "How formal is the draft?", "criteria": ["casual", "neutral", "formal"]},
   "claim": {"type": "noul",   "instructions": "Does the draft state a number without a source?"},
   "next":  {"type": "choice", "instructions": "Which edit comes first?",
             "criteria": {"cut": "Shorten it", "cite": "Add sources", "unknown": "None applies"}}}}
// score: a list of 2-10 levels. choice: an object of 2-64 options. noul: no criteria.
```

### Common errors

| Code | Meaning | Do this |
|---|---|---|
| `WORKSPACE_NOT_ENROLLED` | No active approval covers this folder | Stop calling Jev here. The user can run setup. Never enroll a folder yourself |
| `GENERIC_QUERY_NOT_ENROLLED` | The approval does not include **Advisory Jev tools** | Only `jev_prepare`, `jev_reduce`, `jev_recall` and `jev_auto_status` work. Ask the user to reopen setup if they want more |
| `MCP_ARGUMENTS` | A required field is missing or unknown | Check `workspace_path` and `data_classification` first |
| `ROUTE_CANDIDATES_INVALID`, `RERANK_MEMORIES_INVALID`, `REVIEW_DIFF_INVALID`, `RECIPE_INPUT_INVALID` | Wrong item shape | Fix the shape using the examples above. Do not retry unchanged |
| `SENSITIVE_PAYLOAD_NOT_SENT` or `INPUT_DATA_BLOCKED` | The screen found something that looks like a secret or, in public scope, an email address or home-folder path | Remove it, or use relative paths. Nothing was sent. `jev_reduce` returns the text unchanged with reason `sensitive_data_not_sent` instead |
| `DATA_CLASSIFICATION_NOT_ENROLLED`, `REMOTE_RESTRICTED_DATA` | The request asked for a wider data scope than the approval allows | Use the approved value. Never lower it just to get text through |
| `AUTO_DAILY_BUDGET` | The daily call or byte limit is reached | Continue without Jev today |
| `PROVIDER_TRANSPORT_FAILURE_NO_RETRY`, `PROVIDER_HTTP_*`, `PROVIDER_TIMEOUT` | The provider did not answer cleanly | Continue without Jev and say so in one line. Do not retry in a loop: the attempt may already be billed |
| `BROKER_TIMEOUT` | The request reached the local service but no answer came back in time | The decision may still be running and may be charged. Wait before asking again, and never retry at once |
| `MLX_STATE_WOULD_TRUNCATE`, `MLX_INSTRUCTIONS_WOULD_TRUNCATE` | Laya reads at most 512 tokens per decision (question, options and text together, about 250 to 300 words of text), and this input is longer | Nothing was cut or decided. Pass a shorter excerpt, or use hosted Jev for long text |
| `BROKER_BUSY` | Every slot of the local service was busy for the whole wait | Nothing ran and nothing was charged. It is safe to ask again shortly |
| `BROKER_STORE_DAMAGED` | The local receipts file was damaged while the service ran | Nothing was sent. The service stopped; the next call starts it again with a fresh file |
| `BROKER_UNAVAILABLE`, `BROKER_START_FAILED` | The local service is not running and could not start | Nothing was sent. Continue without Jev; `jev doctor` shows why |

## Which data classification to pass

Pass the value the approval allows. The Claude Code session hook states it when hooks run; otherwise `jev_auto_status` shows it.

| Approved mode | Pass | Where the text goes |
|---|---|---|
| Jev public | `public` | The hosted provider. Emails and home-folder paths are refused |
| Jev reviewed internal | `internal-minimized` | The hosted provider |
| Jev maximum | `restricted` | The hosted provider, for non-secret text you are allowed to share |
| Jev + Laya | `internal-minimized` for ordinary text; `restricted` for private or client content | `internal-minimized` goes to the hosted provider. `restricted` is decided by Laya on this Mac for `jev_route`, `jev_verify`, `jev_rerank`, `jev_review_diff` and `jev_recipe_try`, and is never sent to Jev |
| Laya only | `restricted` | Laya on this Mac. No hosted call is possible |

`provider` is only for `jev_typed_decide`: pass the approved provider (`typesafe`, `openrouter` or `laya-mlx`). Under Jev + Laya, a restricted `jev_typed_decide` also needs `provider: "laya-mlx"`.

## Reading answers

- **Confidence gates, probability does not.** A top probability of 0.85 with confidence 0.55 is not a confident answer. The shipped recipe gates use 0.7 as the confidence floor, and nobody has validated that number for your data.
- **`unknown` is a real answer.** It means "none of your options is supported". Decide the usual way, or rewrite the options once if they were badly described. Never ask the same question again hoping for a different result.
- **`verify` means check it yourself.** Every live recipe result in this release is `verify` or `ignore`, so read `recommendation` and `reasons` for what Jev actually said.
- **`should_abstain: true` means say you do not know.** Do not fall back to the top search hit.
- **Every answer is advice.** No field in any response lets you skip a permission prompt, a test or a human sign-off. Every response carries `"execution_authorized": false`.
- **Report the receipt ID** when the user asks for evidence. `jev_recall` with that ID shows the local record.

## Anti-patterns

1. Calling Jev for a choice with one obvious answer. That adds a round-trip and changes nothing.
2. Forcing your own categories through a recipe with fixed options. Use `jev_route` with your own list instead.
3. Sending a whole file when a sentence or excerpt carries the decision.
4. Making a second routing call just to confirm a suggestion the prompt hook already made.
5. Treating a passing self-test as evidence that Jev is accurate. It only shows that the checking logic is intact.

## Instruction templates for CLAUDE.md, AGENTS.md and Copilot

**You only need a template where hooks do not run.** When the Claude Code plugin's hook runs, it already tells the model the exact arguments at session start and for every subagent; a template on top repeats that every session. Use the template that matches how Jev reaches your assistant, because the tool names differ.

| How Jev is installed | Tool names | Template |
|---|---|---|
| Claude Code plugin, hooks running | `mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__*` | None needed |
| Claude Code plugin, hooks blocked by an organization policy | `mcp__plugin_qualixar-jev-decision-layer_qualixar-jev__*` | [Enterprise-managed Claude Code](HOSTS.md#enterprise-managed-claude-code) |
| Claude desktop app registration (Chat and Code tab), or `claude mcp add` | `mcp__qualixar-jev__*` | [Below](#claude-desktop-app-or-claude-mcp-add) |
| Codex plugin | tools of the `qualixar-jev` server | [Below](#codex-agentsmd) |
| VS Code Copilot agent mode | tools of the `qualixar-jev` server | [Below](#vs-code-copilot-instructions) |

Replace every `<…>` with the values your approval shows. For `data_classification`, see [the table above](#which-data-classification-to-pass).

### Claude desktop app, or `claude mcp add`

Paste into your user `CLAUDE.md` (`~/.claude/CLAUDE.md`, or `CLAUDE.md` in the folder `CLAUDE_CONFIG_DIR` points to).

```markdown
## Qualixar Jev (tools: mcp__qualixar-jev__*)
For a bounded choice, ask Jev instead of re-reasoning. Advisory only: never skip a permission prompt, test or completion check because of an answer.
- workspace_path = the absolute path of the approved folder I am working in (in Chat, the folder I name). Elsewhere Jev returns WORKSPACE_NOT_ENROLLED: then stop calling it. Never create or widen an approval; open jev_setup only when I ask.
- data_classification = <public | internal-minimized | restricted>. Under Jev + Laya: internal-minimized for ordinary text, restricted for private or client content (it stays on this Mac). jev_typed_decide also takes provider = <typesafe | openrouter | laya-mlx>.
- jev_route: 2-12 candidates, each exactly {"id", "description"}; id starts with a letter, [A-Za-z0-9_-], no dots, not "unknown". candidate_id null means decide yourself.
- jev_verify {source_text, extraction}: branch on trustworthy, never on an empty suspect list.
- jev_rerank {query, memories: [{"content", ...}]}: if should_abstain, say I do not have the answer.
- jev_review_diff {goal, diff (unified diff)}: reading order only. jev_recipe_try: exact fields from jev_recipe_catalog.
- Gate on confidence, not top probability. On an error, continue without Jev and say so in one line. No retry loops. Never put secrets in any field.
```

### Codex AGENTS.md

Paste into the project's `AGENTS.md`, or your personal Codex instructions. The Codex session and subagent hooks name the exact arguments when they run; this block gives them to sessions where hooks do not run.

```markdown
## Qualixar Jev (MCP server: qualixar-jev)
For a bounded choice, call a qualixar-jev tool instead of re-reasoning. Answers are advice: never skip approvals, tests or review because of one.
- Always pass workspace_path = the absolute project root and data_classification = <public | internal-minimized | restricted> (under Jev + Laya: restricted for private or client content).
- jev_route: 2-12 candidates, each exactly {"id", "description"} (id: a letter first, [A-Za-z0-9_-], no dots, not "unknown"). candidate_id null means decide yourself.
- jev_verify: branch on trustworthy. jev_rerank: memories need "content"; obey should_abstain. jev_review_diff: a unified diff; reading order only.
- If a prompt already carries a Jev suggestion with a receipt, use it or ignore it. Do not make a second call to confirm it.
- On any error, continue without Jev and say so once. Never retry in a loop, create an approval, or send secrets.
```

### VS Code Copilot instructions

Paste into `.github/copilot-instructions.md`. VS Code has no hooks, so this block is the only way Copilot learns the arguments. Copilot agent-mode tool turns with Jev have **not** been verified yet.

```markdown
## Qualixar Jev (MCP server: qualixar-jev, from .vscode/mcp.json)
Use its tools only for bounded choices: pick from a list (jev_route), check an extraction against its source (jev_verify), check whether retrieved passages answer a question (jev_rerank), or choose where to start a review (jev_review_diff).
Always pass workspace_path = <absolute workspace folder> and data_classification = <public | internal-minimized | restricted>.
jev_route candidates are exactly {"id", "description"}. An id starts with a letter and contains no dots. candidate_id null means decide yourself.
Answers are advice only. On an error, continue without Jev and say so. Never send secrets.
```
