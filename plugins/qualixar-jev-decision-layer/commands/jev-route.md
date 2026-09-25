---
description: Ask Jev to pick one option from a closed candidate list, with a calibrated confidence
argument-hint: '<what you are choosing between, or the task to route>'
allowed-tools: Bash(pwd), Read, Glob, Grep
---

Use a typed Jev decision to choose among a **closed** list of candidates.

Arguments: `$ARGUMENTS`

1. Work out what kind of choice this is: `task`, `tool`, or `skill`.
2. Build a candidate list of 2–12 entries, each with a short stable id and a concrete description of what it does. Gather them from the repository if needed.
3. Call `jev_route` on the `qualixar-jev` MCP server with the workspace path, the kind, the task text, and the candidates.

Reading the answer:

- **Gate on the confidence, not on the top probability.** They are different signals; a 0.85 probability with 0.55 confidence is not a confident answer.
- `unknown`, a low confidence, or a provider error is a **handoff to your own judgment** — not a reason to guess, and not a reason to retry in a loop.
- The result is **advisory**. It never authorises running anything. Decide and act as you normally would, using it as evidence.

State the recommendation, its confidence, and what you are going to do — including when you are overriding it.
