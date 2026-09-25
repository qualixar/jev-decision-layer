---
name: jev-decision-guide
description: Set up or upgrade Qualixar Jev Decision Layer in a workspace, or use bounded task, tool, skill, recipe, review, and context decisions; automatic prompt guidance is separately consented and selective.
---

# Qualixar Jev Decision Layer

This independent Qualixar integration adds typed decisions alongside your coding model. TypeSafe Jev is the hosted route; Laya is an optional local route where installed and verified. The model advises; the agent and native host retain the task, tools and permissions.

1. Check `jev_auto_status` for the current workspace before a live decision. If it is not enrolled, or the user asks to change the saved scope, call `jev_setup` with that exact workspace path. It opens the private two-step browser wizard; only the user enters a provider key and confirms scope there. Never ask for a key in chat or create consent on the user's behalf. A reviewed upgrade keeps the existing Keychain key and daily budget identity when the provider is unchanged.
   If automatic Jev prompt guidance was enabled in the setup review, an eligible coding prompt may already include a receipt-bearing advisory file or skill suggestion. Use that suggestion only if relevant; do not make a duplicate routing call merely to confirm it. No prompt is sent automatically to a hosted provider without that separate opt-in.
2. For a closed task, tool or skill shortlist, call `jev_route` with short candidate IDs and concrete descriptions. Treat `unknown` or a provider error as a handoff to ordinary agent judgment, not a reason to guess or retry indefinitely. The result never authorizes execution.
3. Use `jev_recipe_catalog` to show available use cases. `jev_recipe_try` accepts explicit fields for one recipe and returns experimental advice; the hand-authored fixture examples and uncalibrated thresholds are not model-quality proof.
4. Use `jev_review_diff` only for an explicitly supplied, screened diff. Run the relevant tests and independent code review afterward; Jev's risk and focus answers cannot approve a change.
5. Use `jev_reduce` when a bounded extractive view is useful, and `jev_recall` for exact omitted text. The original source remains authoritative. A shorter view or decision count is not measured Codex token, cost or time saving.

Keep all normal Codex instructions, SuperLocalMemory, existing router and host permission checks intact. If the local broker or provider is unavailable, continue the user's authorized work without silently substituting a fixture. Do not send secrets or whole private repositories to a hosted provider. Restricted non-secret text may be sent only when the exact workspace policy selects Jev maximum; never infer that permission from this skill alone.
