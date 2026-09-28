# Use Jev for a decision, not an entire job

A recipe asks one bounded question about the material you supply. Jev may choose an option, assign a Score, or return a yes/no probability. Your agent or application decides what happens next.

The same recipe definitions run through one shared runtime across Codex, Claude Code, Antigravity, Hermes, and VS Code, but each host adapter has a different surface and verification status. The examples are synthetic and all shipped recipes remain experimental, so inspect the answer before using it on real work.

## Why route a decision here at all

Use a typed decision when a task can be expressed as a bounded question over evidence and explicit choices. A provider call adds its own cost and latency, so whether it reduces overall work must be measured on accepted tasks; this project makes no measured token, cost, or time savings claim.

For a live `jev_recipe_try` in 1.0.8, the packaged local gate evaluates the provider answer and returns a `host_action` plus a policy receipt ID. The response is advisory and never executes the recommendation:

- **`act`** — the answer passed the configured gate. Every shipped recipe is `SPECIFICATION_NOT_MODEL_EVALUATED`, so a would-be `act` is capped to `verify` in 1.0.8.
- **`verify`** — independently check the advisory answer. This is the highest result a shipped recipe can return when its answer would otherwise pass.
- **`ignore`** — the model selected `unknown`. Do not use a recommendation; decide normally.

A below-threshold or malformed answer remains `verify`. The policy receipt records the recipe status, provider receipt ID, provider/model, and gate result. It is evidence of the local policy result, not evidence that a provider is accurate or that an action was performed.

**Confidence summarizes the probability distribution.** [TypeSafe computes Choice and Score confidence from their reported probabilities](https://docs.typesafe.ai/confidence); it is not an independent accuracy measurement. The gate applies its configured confidence floor and probability bar as unvalidated policy settings, and a live passing recipe remains capped to `verify`. A Noul answer has no confidence field; its yes/no bands apply to its value.

## For managers and team leads

- **Prioritize a work item** (`qualixar.work-item-priority`): compare one item with an explicit rubric and supplied team priorities. Treat the score as a suggestion for discussion; the recipe cannot see the full roadmap, staffing context, or private project system.
- **Route a meeting action** (`qualixar.meeting-action-routing`): suggest a responsible team from a taxonomy you provide. A manager confirms ownership and communicates the assignment.
- **Review release evidence** (`qualixar.release-readiness`): check supplied evidence against one requirement. It does not inspect the repository, run a build, or authorize a release.

## For everyday and freelance work

- **Sort a new enquiry** (`qualixar.lead-routing`): match an enquiry to the service category you supplied. You own the category list and whether anyone is contacted.
- **Check an invoice exception** (`qualixar.invoice-exception-triage`): decide which of your stated reconciliation paths deserves review. Code still computes totals and validates account records.
- **Turn meeting notes into an action queue** (`qualixar.meeting-action-routing`): suggest a responsible team from an explicit taxonomy. It does not send a message or assign work automatically.
- **Check proposal requirements** (`qualixar.proposal-requirements`): judge whether a supplied excerpt addresses a named requirement, while keeping missing evidence visible.
- **Route a support issue** (`qualixar.support-triage`): choose from the support categories you provide. An uncertain answer stays with a person.

## For content creators

- **Choose a content format** (`qualixar.content-repurpose`): select from formats you provide, such as a short video, newsletter, or carousel, using the supplied brief and audience. It does not generate or publish the content.
- **Brief fit** (`qualixar.brief-fit`): compare a draft excerpt with a supplied brief.
- **Brand tone** (`qualixar.brand-tone`): score an excerpt against an explicit style requirement, not against an invented personality profile.
- **Publication review** (`qualixar.publication-review`): suggest whether a draft needs editorial review before publishing. It cannot fact-check an unsupported claim by itself.

Even when a brief-fit answer clears its configured gate, the shipped recipe status means the live result still requires verification. The specification has not yet been evaluated against labeled provider answers.

## For research and knowledge work

Use `qualixar.claim-verification`, `qualixar.citation-check`, `qualixar.research-ranking`, `qualixar.source-deduplication`, `qualixar.context-sieve`, and `qualixar.memory-admission` to screen supplied evidence or candidate memories. Keep the source documents available. A judgment about a citation does not replace opening the cited source.

## For software teams

Closed routing contracts cover tasks, tools, skills, workers, files, tests and review scopes. Other recipes address incident and failure classification, injection triage, semantic lint, documentation drift, patch review and completion evidence. Use `jev_route` when you already have a concrete candidate list; `jev_review_diff` for advisory review focus; `jev_recipe_catalog` to inspect the full catalog. None grants permission to execute a tool or approve a patch.

## For delivery and agent loops

- **Release readiness** (`qualixar.release-readiness`): judge whether supplied evidence satisfies one stated release requirement — version agreement across manifests, a changelog entry, test evidence. It reads the evidence you give it; it does not inspect the repository, run a build, or approve a publish.
- **Retry or stop** (`qualixar.retry-decision`): given a failure and what has already been tried, choose among the actions you supplied. It never re-runs a command, and the retry budget stays yours.
- **Disclosure check** (`qualixar.disclosure-check`): does this commit message or release note say more than the change requires? Best-effort on supplied text, not a secret scanner.
- **Output relevance** (`qualixar.output-relevance`): before the host reads a long tool result, decide whether it contains anything that answers the goal. It judges relevance, not correctness, and nothing is discarded — the original output stays authoritative.

## For browser tasks

The `jev-browser-choice` skill uses the browser interface already available in the host. It skips Jev when the next safe control is obvious. When several observed, benign controls plausibly serve the same goal, it can send only their short labels and a concise goal to `jev_route`; for an uncertain operation and target, `jev_typed_decide` can ask both Choice questions in one request. The host still validates the selected control against the current page, performs the click under its native permissions, and verifies the result. It does not hand Jev a screenshot, hidden fields, selectors, credentials, checkout controls, or authority to purchase. One call may improve a difficult choice; that is not a measured token- or time-savings claim.

## A simple first session for a non-developer

1. Ask your agent to show the recipe catalog and explain one recipe in plain language.
2. Use the local workbench or offline fixture to see the question, the input fields, and an example result without sending text to a provider.
3. Try a live recipe only after reviewing the workspace, provider, and data scope in setup. Start with public synthetic material.
4. Read the returned `host_action` and receipt. In 1.0.8, every would-be passing recipe result is capped to `VERIFY` until that recipe has been evaluated against labeled provider answers. Decide and act through your host as usual.

## Try a recipe safely

Offline first, with no provider charge and nothing enrolled:

```sh
plugins/qualixar-jev-decision-layer/scripts/jev selftest --recipe qualixar.brief-fit --variant nominal
```

Then, after workspace setup, ask your agent: **"Show the Qualixar Jev recipe catalog, then try the brief-fit recipe on this public synthetic example."** It can call `jev_recipe_catalog` and `jev_recipe_try` without you writing JSON. In 1.0.8 the live call applies the packaged local gate and returns an `EXPERIMENTAL_ADVISORY` result with `host_action` and `policy_receipt_id`. A would-be `ACT` is capped to `VERIFY` while the recipe remains `SPECIFICATION_NOT_MODEL_EVALUATED`; an explicit `unknown` may return `IGNORE`. The agent still decides whether to act and performs any authorized action itself.

## Contributing a recipe

Each new recipe declares its audience, exact input fields, typed question, allowed advisory outcome, and limits, and ships three synthetic fixtures — nominal, uncertain, adversarial. Fixtures live in `fixtures/<recipe-id>.json`, **not** inside the recipe: a public recipe is a specification, and a hand-written answer sitting in one reads as evidence of what the provider does. Source recipes live under `recipes/<audience>/`.

After a reviewed edit, from the repository root:

```sh
python3 tools/build_recipes.py
python3 -m unittest discover -s tests -q
```

The build produces the plugin's sanitized public runtime catalog, with gates and fixtures as separate arrays beside the recipes — the model is never shown its own threshold, nor a worked example of the answer it is being asked for. The build does not publish or call Jev. See [CONTRIBUTING](../CONTRIBUTING.md) for the full checklist.
