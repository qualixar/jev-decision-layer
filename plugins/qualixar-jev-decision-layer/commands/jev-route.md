---
description: Ask Jev to pick one option from a closed candidate list, with a confidence score
argument-hint: '<what you are choosing between, or the task to route>'
allowed-tools: Bash(pwd), Read, Glob, Grep
---

Use a typed Jev decision to choose among a **closed** list of candidates.

Arguments: `$ARGUMENTS`

1. Work out what kind of choice this is: `task`, `tool`, or `skill`. Use `task` for a model tier, a workflow or a team.
2. Build a candidate list of 2–12 entries. Each entry is exactly `{"id": ..., "description": ...}` and nothing else: the id starts with a letter and uses only letters, digits, `_` or `-` (no dots or colons, up to 64 characters, never `unknown`), and the description is 1–250 characters saying concretely what that option does. Gather them from the repository if needed.
3. Call `jev_route` on the `qualixar-jev` MCP server with:
   - `workspace_path`: the absolute path of the current workspace;
   - `kind`, `task` (the task text), and `candidates`;
   - `data_classification`: the value this workspace's grant accepts, as stated in the Jev session guidance or by `jev_auto_status` (`public`, `internal-minimized` or `restricted`). Under a Jev + Laya grant, pass `restricted` when the task or candidates carry private or client content: Laya then decides it on this Mac and nothing is sent to Jev. Never pass a wider value than the grant allows.

Reading the answer:

- **Gate on the confidence, not on the top probability.** They are different signals; a 0.85 probability with 0.55 confidence is not a confident answer.
- `status: ABSTAIN_UNKNOWN` (with `candidate_id` null), a low confidence, or a provider error is a **handoff to your own judgment** — not a reason to guess, and not a reason to retry in a loop. `ROUTE_CANDIDATES_INVALID` means the candidate shape is wrong: fix it once, do not resend it unchanged.
- The result is **advisory**. It never authorises running anything. Decide and act as you normally would, using it as evidence.

State the recommendation, its confidence, the receipt ID, and what you are going to do — including when you are overriding it.
