# Contributing

The fastest useful contribution is one bounded recipe for a task people actually repeat. Put its JSON file under `recipes/<audience>/`, use a unique `qualixar.*` ID, define exact input fields and one typed decision question, and include a clear `unknown` or review path. Keep it advisory; do not add executable instructions or automatic tool authority to a recipe.

Run the local checks after editing:

```sh
python3 tools/build_recipes.py
python3 -m unittest discover -s tests -q
```

`build_recipes.py` validates the source contracts and refreshes only the sanitized catalog in the plugin runtime. Commit the recipe and regenerated catalog/manifest together. Tests should cover a normal synthetic example, an uncertain example, missing input and adversarial text. Do not use real credentials, customer records or private repository excerpts in fixtures or issues.

For core code, keep provider credentials out of logs and model-facing responses; validate closed options and answer identity before acting; preserve the host's permissions and original tool output. A passing fixture does not establish model quality. Any live TypeSafe/OpenRouter test must be opt-in, use synthetic data and state its call limit.

Before copying upstream code or assets, verify the exact project's license and retain its notice. Pattern inspiration is not code reuse. Describe what you changed, what you ran, and which host/provider cells remain untested. Do not state token, cost or time savings without a reproducible accepted-task measurement.
