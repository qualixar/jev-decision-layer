---
description: Triage a diff with Jev to suggest review focus and risk areas
argument-hint: '[base ref, defaults to the unstaged working tree]'
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(pwd), Read
---

Use Jev to decide **where to look first** in a change.

Arguments: `$ARGUMENTS`

1. Collect the diff. Use `git diff $ARGUMENTS` when a base ref is given, otherwise the unstaged working tree.
2. If the diff exceeds the tool's limit, review it in parts rather than truncating silently — and say that you split it.
3. Call `jev_review_diff` on the `qualixar-jev` MCP server with the workspace path, the review goal, and the diff text.

**This never approves code and never runs Git for you.** It suggests focus and risk. Do the actual review yourself against what it flags, and report anything it missed that you found — that gap is the useful signal.
