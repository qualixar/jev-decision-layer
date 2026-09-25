# Use Jev for a decision, not an entire job

A recipe asks one bounded question about the material you supply. Jev may choose an option, assign a Score, or return a yes/no probability. Your agent or application decides what happens next.

Everything on this page behaves identically in Codex, Claude Code, Antigravity, Hermes and VS Code — one runtime answers the question and only the adapter that carries it differs, so a recipe does not need re-learning when you change harness. The examples are synthetic and the newer recipes are experimental, so inspect the answer before using it on real work.

## Why route a decision here at all

A typed decision costs a fraction of what it costs your coding model to reason the same question out in its own context. The saving is not the call — it is the work the agent then does not do: no file sweep to guess which tests matter, no second pass re-deriving whether a claim was supported, no re-reading a diff to decide where review effort belongs.

That economy only holds if the answer arrives usable. Every answer is gated locally before the host sees it and carries a `host_action`:

- **`act`** — cleared both the confidence floor and the distribution bar. Use it; re-deriving the judgment is the cost the call was made to avoid.
- **`verify`** — a starting point, not a conclusion. Cheaper to check than to work out from nothing.
- **`ignore`** — below the floor, or the model chose `unknown`. Decide normally. The call still earned its keep by removing a bad option.

**Confidence is not probability.** A distribution can look decisive while the answer is not calibrated. The gate applies both, so do not second-guess it from the top probability alone. A Noul answer has no confidence field at all — its distance from 0.5 is the certainty, and the yes/no bands express it.

## For everyday and freelance work

- **Sort a new enquiry** (`qualixar.lead-routing`): match an enquiry to the service category you supplied. You own the category list and whether anyone is contacted.
- **Check an invoice exception** (`qualixar.invoice-exception-triage`): decide which of your stated reconciliation paths deserves review. Code still computes totals and validates account records.
- **Turn meeting notes into an action queue** (`qualixar.meeting-action-routing`): suggest a responsible team from an explicit taxonomy. It does not send a message or assign work automatically.
- **Check proposal requirements** (`qualixar.proposal-requirements`): judge whether a supplied excerpt addresses a named requirement, while keeping missing evidence visible.
- **Route a support issue** (`qualixar.support-triage`): choose from the support categories you provide. An uncertain answer stays with a person.

## For content creators

- **Brief fit** (`qualixar.brief-fit`): compare a draft excerpt with a supplied brief.
- **Brand tone** (`qualixar.brand-tone`): score an excerpt against an explicit style requirement, not against an invented personality profile.
- **Publication review** (`qualixar.publication-review`): suggest whether a draft needs editorial review before publishing. It cannot fact-check an unsupported claim by itself.

Copy that already matches the brief reports a passed check rather than routing you to a review you do not need.

## For research and knowledge work

Use `qualixar.claim-verification`, `qualixar.citation-check`, `qualixar.research-ranking`, `qualixar.source-deduplication`, `qualixar.context-sieve`, and `qualixar.memory-admission` to screen supplied evidence or candidate memories. Keep the source documents available. A judgment about a citation does not replace opening the cited source.

## For software teams

Closed routing contracts cover tasks, tools, skills, workers, files, tests and review scopes. Other recipes address incident and failure classification, injection triage, semantic lint, documentation drift, patch review and completion evidence. Use `jev_route` when you already have a concrete candidate list; `jev_review_diff` for advisory review focus; `jev_recipe_catalog` to inspect the full catalog. None grants permission to execute a tool or approve a patch.

## For browser tasks

The `jev-browser-choice` skill uses the browser interface already available in the host. It skips Jev when the next safe control is obvious. When several observed, benign controls plausibly serve the same goal, it can send only their short labels and a concise goal to `jev_route`; for an uncertain operation and target, `jev_typed_decide` can ask both Choice questions in one request. The host still validates the selected control against the current page, performs the click under its native permissions, and verifies the result. It does not hand Jev a screenshot, hidden fields, selectors, credentials, checkout controls, or authority to purchase. One call may improve a difficult choice; that is not a measured token- or time-savings claim.

## Try a recipe safely

Offline first, at no cost and with nothing enrolled:

```sh
python3 -m jev_auto.cli selftest --recipe qualixar.brief-fit --variant nominal
```

Then, after workspace setup, ask your agent: **"Show the Qualixar Jev recipe catalog, then try the brief-fit recipe on this public synthetic example."** It can call `jev_recipe_catalog` and `jev_recipe_try` without you writing JSON. The tool returns a receipt and marks the result `EXPERIMENTAL_ADVISORY`.

## Contributing a recipe

Each new recipe declares its audience, exact input fields, typed question, allowed advisory outcome, and limits, and ships three synthetic fixtures — nominal, uncertain, adversarial. Fixtures live in `fixtures/<recipe-id>.json`, **not** inside the recipe: a public recipe is a specification, and a hand-written answer sitting in one reads as evidence of what the provider does. Source recipes live under `recipes/<audience>/`.

After a reviewed edit, from the repository root:

```sh
python3 tools/build_recipes.py
python3 -m unittest discover -s tests -q
```

The build produces the plugin's sanitized public runtime catalog, with gates and fixtures as separate arrays beside the recipes — the model is never shown its own threshold, nor a worked example of the answer it is being asked for. The build does not publish or call Jev. See [CONTRIBUTING](../CONTRIBUTING.md) for the full checklist.
