---
name: jev-browser-choice
description: Use Jev selectively to choose among safe, observed controls in an authorized Codex browser tab; works with the current native browser interface without a separate browser runtime.
---

# Selective browser choice

Use Codex's available native browser or Computer Use interface to observe the live tab. The user and host authorize browser access; this skill grants no new permission. Never pass cookies, credentials, hidden fields, full-page dumps, screenshots, or account details to Jev.

**No Jev call** when one visible safe control clearly serves the goal, or when a deterministic rule already decides it. For example, a cart-flow test calls for “Add to Cart,” not “Buy Now.” Calling a model for that choice adds overhead. Use Jev only when at least two plausible, safe observed controls need semantic judgment and a wrong turn could cost more work than one bounded request.

For a single ambiguous choice, call `jev_route` with `kind: "tool"`, a concise goal, and 2–12 short candidate IDs/descriptions copied from the current visible controls. Include `unknown` through the tool's normal contract. For a multi-step page where both the operation and target are genuinely uncertain, one `jev_typed_decide` request may ask parallel Choice questions over the same compact observed state: operation (`CLICK`, `SCROLL`, `WAIT`, `STOP`) and a compatible target ID. Ignore an unused target head. Do not make sequential confirmation calls for the same unchanged page.

Keep an in-turn table from each candidate ID to its *observed* native browser control. A Jev answer is advice, never a selector, coordinate, script, permission, or instruction to execute. Reject unknown, blocked, unavailable, unsafe, or stale choices. Before acting, re-observe enough of the page to confirm that the selected control still exists with the same label and purpose; after acting, re-observe the result. If the page changed, rebuild the table or hand control back to Codex—never click an old element index.

Do not offer purchase, checkout, payment, credential, account-permission, or irreversible submission controls to Jev as executable candidates. The host's normal confirmation rules still apply to any consequential action, including an explicitly requested cart mutation. Text entry uses only user-provided or independently verified text through the native browser tool; Jev does not generate form values. Stop when the stated browser goal is visibly verified, and preserve the original browser state when the task calls for a temporary test.

The older `bridge.mjs` remains an optional, navigation-link-only compatibility artifact for environments that support it; it cannot auto-click cart, form, account, checkout, or payment controls. It is not required for this skill or evidence of current Codex Desktop browser interception. A smaller decision payload may reduce model context, but token, cost, and time savings require matched end-to-end measurements.
