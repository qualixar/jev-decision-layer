---
description: Show this workspace's Jev enrolment, provider route and actual counters
argument-hint: '[workspace path, defaults to the current directory]'
allowed-tools: Bash(pwd)
---

Report the Qualixar Jev Decision Layer status for this workspace.

Arguments: `$ARGUMENTS`

Call `jev_auto_status` on the `qualixar-jev` MCP server with the workspace path (`$ARGUMENTS` or the current directory) and report exactly what it returns: enrolment state, provider route, and the counters it actually measured.

**Report measured numbers only.** Do not estimate, extrapolate or infer token or cost savings — the layer deliberately does not promise them without measurement. If a counter is absent, say it was not measured rather than guessing a value.
