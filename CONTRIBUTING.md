# Contributing

The fastest useful contribution is one bounded recipe for a task people actually repeat. Put its JSON file under `recipes/<audience>/`, use a unique `qualixar.*` ID, define exact input fields and one typed decision question, and include a clear `unknown` or review path. Keep it advisory; do not add executable instructions or automatic tool authority to a recipe.

## Fixtures are required, and they live outside the recipe

Every recipe ships three synthetic cases in `fixtures/<recipe-id>.json`:

- **nominal** — a clear-cut input the gate must clear, so the host acts once.
- **uncertain** — a genuinely ambiguous input the gate must **not** clear. Make it fail for the reason you intend: a choice case with a decisive distribution and poor confidence isolates the confidence floor, while one that also misses the probability bar proves nothing about it.
- **adversarial** — an input carrying an instruction in material that is supposed to be data. The gate must refuse to authorise; a layer that can be talked into `act` is worse than no layer.

Fixtures do **not** go inside the recipe file, and a test enforces that. A public recipe is a specification, and a hand-authored answer sitting in one reads as evidence of what the provider does. In the built catalog they are a separate array beside the recipes, for the same reason gates are: the model must never be shown its own threshold, nor a worked example of the answer it is being asked for.

Record `expected_status`, `expected_host_action` and `expected_recommendation` as the real gate's output for your mock answer, and check that output matches the variant's intent before committing it. Freezing an expectation you did not verify produces a test that passes for the wrong reason.

## Run the local checks

```sh
python3 tools/build_recipes.py
python3 -m unittest discover -s tests -q
plugins/qualixar-jev-decision-layer/scripts/jev selftest
```

`build_recipes.py` validates the source contracts and refreshes only the sanitized catalog in the plugin runtime. Commit the recipe, its fixture file and the regenerated catalog/manifest together. A new runtime file also needs its hash in `RUNTIME_MANIFEST.json` **and** a `git add` — the manifest test compares against `git ls-files`, so an unstaged file fails in a way that looks unrelated.

Do not use real credentials, customer records or private repository excerpts in fixtures or issues.

## Host adapters

One runtime, one shim per harness. Do not duplicate decision logic and do not vendor a second runtime. Hook and config files are per host and are never merged — editing Codex's `hooks/hooks.json` to satisfy another harness breaks Codex silently, and its tests will catch you rather than a reviewer. The full checklist for adding a harness is in [the host guide](docs/HOSTS.md).

`plugins/qualixar-jev-decision-layer/` is the canonical portable source. `plugins/qualixar-jev-codex/` is generated from it by `tools/build_codex_package.py`; never hand-edit it.

## Core code

Keep provider credentials out of logs and model-facing responses; validate closed options and answer identity before acting; preserve the host's permissions and original tool output. A passing fixture does not establish model quality. Any live TypeSafe/OpenRouter test must be opt-in, use synthetic data and state its call limit.

Before copying upstream code or assets, verify the exact project's license and retain its notice. Pattern inspiration is not code reuse. Describe what you changed, what you ran, and which host/provider cells remain untested. Do not state token, cost or time savings without a reproducible accepted-task measurement.
