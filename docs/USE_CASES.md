# Use Jev for a decision, not an entire job

A recipe asks one bounded question about the material you supply. Jev may choose an option, assign a Score, or return a yes/no probability. Codex or your application decides what happens next. The included examples are synthetic and the new recipes are experimental; inspect the answer before using it on real work.

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

## For research and knowledge work

Use `qualixar.claim-verification`, `qualixar.citation-check`, `qualixar.research-ranking`, `qualixar.source-deduplication`, `qualixar.context-sieve`, and `qualixar.memory-admission` to screen supplied evidence or candidate memories. Keep the source documents available. A Jev judgment about a citation does not replace opening the cited source.

## For software teams

Closed routing contracts cover tasks, tools, skills, workers, files, tests and review scopes. Other recipes address incident and failure classification, injection triage, semantic lint, documentation drift, patch review and completion evidence. Use `jev_route` when you already have a concrete candidate list; use `jev_review_diff` for advisory review focus; use `jev_recipe_catalog` to inspect the full catalog. None grants permission to execute a tool or approve a patch.

## For browser tasks

The `jev-browser-choice` skill uses the browser interface already available in Codex. It skips Jev when the next safe control is obvious. When several observed, benign controls plausibly serve the same goal, it can send only their short labels and a concise goal to `jev_route`; for an uncertain operation and target, `jev_typed_decide` can ask both Choice questions in one request. Codex still validates the selected control against the current page, performs the click under its native permissions, and verifies the result. It does not hand Jev a screenshot, hidden fields, selectors, credentials, checkout controls, or authority to purchase. One Jev call may improve a difficult choice; this is not a measured token- or time-savings claim.

## Try a recipe safely

After workspace setup, ask Codex: “Show the Qualixar Jev recipe catalog, then try the brief-fit recipe on this public synthetic example.” Codex can call `jev_recipe_catalog` and `jev_recipe_try` without you writing JSON. The tool returns a receipt and marks the result `EXPERIMENTAL_ADVISORY`.

For a contributor, each new recipe must declare its audience, exact input fields, typed question, allowed advisory outcome, synthetic nominal/uncertain/missing/adversarial fixtures, and limits. The catalog accepts reviewed additions without modifying the runtime's recipe count. It does not activate automatic actions or certify a provider's accuracy.

The source recipe files live under `recipes/`. After a reviewed edit, run `python3 tools/build_recipes.py` and `python3 -m unittest discover -s tests -q` from the repository root. The build produces the plugin's smaller public runtime catalog; it does not publish or call Jev.
