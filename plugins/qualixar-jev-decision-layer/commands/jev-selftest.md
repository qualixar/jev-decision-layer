---
description: Replay the shipped synthetic fixtures through the local gate — offline, no provider call, no key
argument-hint: '[recipe id and variant, e.g. qualixar.patch-review adversarial]'
---

Check the Qualixar Jev decision gate without spending anything.

Arguments: `$ARGUMENTS`

Call `jev_recipe_selftest` on the `qualixar-jev` MCP server. With no arguments it replays all 96 shipped fixtures — 32 recipes across nominal, uncertain and adversarial — and returns the counts. Given a recipe id (and optionally a variant) it replays that single case and shows expected against observed.

This is fully offline: no provider is contacted, no key is used, and the workspace does not need to be enrolled. Run it when adopting the layer, or when a live gate result looks wrong and you need to know whether the gate itself is sound.

Report what it returns. If `all_passed` is true, say so and give the counts. If any case failed, show its `expected` and `observed` blocks and the `reasons` — a failure means the shipped gate no longer matches its recorded contract, which is a defect in the layer, not in the caller's input.

**This is a contract check, not a benchmark.** The fixtures are hand-authored synthetic cases. Never present a passing run as evidence of Jev's accuracy, or as a measured token, cost or time saving.
