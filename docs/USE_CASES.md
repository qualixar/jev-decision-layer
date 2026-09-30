# Use Jev for a decision, not an entire job

A recipe asks one bounded question about the material you supply. Jev may choose an option, assign a Score, or return a yes/no probability. Your agent or application decides what happens next.

The same recipe definitions run through one shared runtime across Codex, Claude Code, Antigravity, Hermes, and VS Code, but each host adapter has a different surface and verification status. The examples are synthetic and all shipped recipes remain experimental, so inspect the answer before using it on real work.

## Why route a decision here at all

Use a typed decision when a task can be expressed as a bounded question over evidence and explicit choices. A provider call adds its own cost and latency, so whether it reduces overall work must be measured on accepted tasks; this project makes no measured token, cost, or time savings claim.

**Recipes with fixed options choose only among their own options.** A routing or triage recipe such as lead routing or support triage has a fixed list of answers, shown below and in `jev_recipe_catalog`. The description of your teams or categories that you pass in is context for that choice; it does not add options. If your own list has other names, ask your assistant to use Jev with your list, which calls `jev_route` with your candidates. You can ask for this in plain words; you do not write JSON. The exact argument shapes and per-person playbooks are in [Working with Jev](WORKING_WITH_JEV.md).

For a live `jev_recipe_try`, the packaged local gate evaluates the provider answer and returns a `host_action` plus a policy receipt ID. The response is advisory and never executes the recommendation:

- **`act`** — the answer passed the configured gate. Every shipped recipe is `SPECIFICATION_NOT_MODEL_EVALUATED`, so a would-be `act` is capped to `verify` in this release.
- **`verify`** — independently check the advisory answer. This is the highest result a shipped recipe can return when its answer would otherwise pass.
- **`ignore`** — the model selected `unknown`. Do not use a recommendation; decide normally.

A below-threshold or malformed answer remains `verify`. The policy receipt records the recipe status, provider receipt ID, provider/model, and gate result. It is evidence of the local policy result, not evidence that a provider is accurate or that an action was performed.

**Confidence summarizes the probability distribution.** [TypeSafe computes Choice and Score confidence from their reported probabilities](https://docs.typesafe.ai/confidence); it is not an independent accuracy measurement. The gate applies its configured confidence floor and probability bar as unvalidated policy settings, and a live passing recipe remains capped to `verify`. A Noul answer has no confidence field; its yes/no bands apply to its value.

## For managers and team leads

- **Prioritize a work item** (`qualixar.work-item-priority`): compare one item with an explicit rubric and supplied team priorities. Treat the score as a suggestion for discussion; the recipe cannot see the full roadmap, staffing context, or private project system.
- **Route a meeting action** (`qualixar.meeting-action-routing`): suggest one of four fixed queues — `content`, `customer_support`, `engineering` or `operations` — for one explicit action sentence, using the team responsibilities you describe. For your own team names, use `jev_route` with your list. A manager confirms ownership and communicates the assignment.
- **Review release evidence** (`qualixar.release-readiness`): check supplied evidence against one requirement. It does not inspect the repository, run a build, or authorize a release.

## For everyday and freelance work

- **Sort a new enquiry** (`qualixar.lead-routing`): suggest one of four fixed service queues — `consulting`, `general`, `partnership` or `product_help` — using the service categories you describe. For your own category names, use `jev_route` with your list. You decide whether anyone is contacted.
- **Check an invoice exception** (`qualixar.invoice-exception-triage`): suggest which fixed review path fits the reconciliation flags you supply — `amount_review`, `duplicate_review`, `purchase_order_review` or `vendor_review`. Code still computes totals and validates account records.
- **Turn meeting notes into an action queue** (`qualixar.meeting-action-routing`): one action sentence at a time, into the four fixed queues above. It does not send a message or assign work automatically.
- **Check proposal requirements** (`qualixar.proposal-requirements`): judge whether a supplied excerpt addresses a named requirement, while keeping missing evidence visible.
- **Route a support issue** (`qualixar.support-triage`): suggest one of four fixed categories — `account_help`, `billing_review`, `sales` or `technical` — using the support taxonomy you describe. For your own categories, use `jev_route`. An uncertain answer stays with a person.

## For content creators

- **Choose a content format** (`qualixar.content-repurpose`): suggest one of four fixed formats — `short_video`, `newsletter`, `carousel` or `short_post` — from the summary, audience, goal, production constraints and rights status you supply. Unclear rights give `unknown`. For your own formats, use `jev_route`. It does not generate or publish the content.
- **Brief fit** (`qualixar.brief-fit`): score a draft excerpt against a supplied brief on three levels: does not, partly, or directly meets it.
- **Brand tone** (`qualixar.brand-tone`): score an excerpt against an explicit style requirement, not against an invented personality profile.
- **Publication review** (`qualixar.publication-review`): suggest which review a draft needs against your checklist — `editor_review`, `evidence_review`, `privacy_review` or `disclosure_review`. It cannot fact-check an unsupported claim by itself.

Even when a brief-fit answer clears its configured gate, the shipped recipe status means the live result still requires verification. The specification has not yet been evaluated against labeled provider answers.

## For research and knowledge work

Use `qualixar.claim-verification`, `qualixar.citation-check`, `qualixar.research-ranking`, `qualixar.source-deduplication`, `qualixar.context-sieve`, and `qualixar.memory-admission` to screen supplied evidence or candidate memories. Keep the source documents available. A judgment about a citation does not replace opening the cited source.

## For software teams

Closed routing contracts cover tasks, tools, skills, workers, files, tests and review scopes. Other recipes address incident and failure classification, injection triage, semantic lint, documentation drift, patch review and completion evidence. Use `jev_route` when you already have a concrete candidate list; `jev_review_diff` for advisory review focus; `jev_recipe_catalog` to inspect the full catalog. None grants permission to execute a tool or approve a patch.

## For delivery and agent loops

- **Release readiness** (`qualixar.release-readiness`): judge whether supplied evidence satisfies one stated release requirement — version agreement across manifests, a changelog entry, test evidence. It reads the evidence you give it; it does not inspect the repository, run a build, or approve a publish.
- **Retry or stop** (`qualixar.retry-decision`): given a failure and what has already been tried, suggest one of four fixed actions — `retry_unchanged`, `retry_with_change`, `stop` or `escalate`. It never re-runs a command, and the retry budget stays yours.
- **Disclosure check** (`qualixar.disclosure-check`): does this commit message or release note say more than the change requires? Best-effort on supplied text, not a secret scanner.
- **Output relevance** (`qualixar.output-relevance`): before the host reads a long tool result, decide whether it contains anything that answers the goal. It judges relevance, not correctness, and nothing is discarded — the original output stays authoritative.

## For browser tasks

The `jev-browser-choice` skill uses the browser interface already available in the host. It skips Jev when the next safe control is obvious. When several observed, benign controls plausibly serve the same goal, it can send only their short labels and a concise goal to `jev_route`; for an uncertain operation and target, `jev_typed_decide` can ask both Choice questions in one request. The host still validates the selected control against the current page, performs the click under its native permissions, and verifies the result. It does not hand Jev a screenshot, hidden fields, selectors, credentials, checkout controls, or authority to purchase. One call may improve a difficult choice; that is not a measured token- or time-savings claim.

## A simple first session for a non-developer

Someone comfortable with a terminal installs the plugin once (see the [README](../README.md#install-and-upgrade)). After that, everything below is a plain-words request to your assistant.

1. Ask your agent to show the recipe catalog and explain one recipe in plain language.
2. Ask it to run that recipe's offline self-test, to see the question, the input fields, and an example result without sending text to a provider.
3. Try a live recipe only after reviewing the workspace, provider, and data scope in setup. Start with public synthetic material.
4. Read the returned `recommendation`, `confidence` and `reasons`. In this release every would-be passing recipe result is capped to `verify` until that recipe has been evaluated against labeled provider answers. Decide and act through your host as usual.

[Working with Jev](WORKING_WITH_JEV.md#playbooks) has a step-by-step first session for creators, managers and developers.

## Try a recipe safely

Offline first, with no provider charge and nothing enrolled:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest --recipe qualixar.brief-fit --variant nominal
```

Then, after workspace setup, ask your agent: **"Show the Qualixar Jev recipe catalog, then try the brief-fit recipe on this public synthetic example."** It can call `jev_recipe_catalog` and `jev_recipe_try` without you writing JSON. The live call applies the packaged local gate and returns an `EXPERIMENTAL_ADVISORY` result with `host_action` and `policy_receipt_id`. A would-be `ACT` is capped to `VERIFY` while the recipe remains `SPECIFICATION_NOT_MODEL_EVALUATED`; an explicit `unknown` may return `IGNORE`. The agent still decides whether to act and performs any authorized action itself.

## Contributing a recipe

Each new recipe declares its audience, exact input fields, typed question, allowed advisory outcome, and limits, and ships three synthetic fixtures — nominal, uncertain, adversarial. Fixtures live in `fixtures/<recipe-id>.json`, **not** inside the recipe: a public recipe is a specification, and a hand-written answer sitting in one reads as evidence of what the provider does. Source recipes live under `recipes/<audience>/`.

After a reviewed edit, from the repository root:

```sh
python3 tools/build_recipes.py
python3 -m unittest discover -s tests -q
```

The build produces the plugin's sanitized public runtime catalog, with gates and fixtures as separate arrays beside the recipes — the model is never shown its own threshold, nor a worked example of the answer it is being asked for. The build does not publish or call Jev. See [CONTRIBUTING](../CONTRIBUTING.md) for the full checklist.
