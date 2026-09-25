---
name: jev-browser-choice
description: Use Jev to choose a short sequence of safe controls in an existing, already-authorized Codex Computer Use browser tab; never create a new browser or bypass native permissions.
---

# Existing-tab browser choice

Start with Codex's installed Computer Use skill and obtain the authorized live tab exactly as that skill requires. This bridge works only inside that existing tab; it never launches another browser, reads hidden form fields or turns model output into selectors or JavaScript.

Load `bridge.mjs` from this installed skill directory in the Computer Use runtime. The runtime must support local `node:fs` and `node:net`; otherwise return `BRIDGE_IPC_UNAVAILABLE` and continue through ordinary Computer Use. Call `loadConfig` with the exact enrolled workspace path, then `createSession` with the observed tab, a narrow goal, a small action budget and only observed safe controls. If a custom `XDG_CONFIG_HOME` was used, pass that exact directory to `loadConfig`.

The defaults discover navigation labels. Explicit benign controls must use their exact observed names. Do not type credentials, submit purchases or forms, infer hidden elements, send cookies, or ask Jev to interpret screenshots. A stale page, uncertain choice, unavailable control or `DONE` judgment returns control to Codex. Inspect the final page with the native skill before saying the user's goal was met.

The local bridge has offline safety tests; an installed-host browser run is a separate verification step. Its output is a navigation suggestion, not proof that all Codex browser actions were intercepted.
