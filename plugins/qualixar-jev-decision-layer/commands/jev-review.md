---
description: Triage a diff with Jev to suggest review focus and risk areas
argument-hint: '[base ref, defaults to the unstaged working tree]'
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(pwd), Read
---

Use Jev to decide **where to look first** in a change.

Arguments: `$ARGUMENTS`

1. Collect the diff. Use `git diff $ARGUMENTS` when a base ref is given, otherwise the unstaged working tree. Send it as a unified diff, with its `---` and `+++` lines; anything else is refused as `REVIEW_DIFF_INVALID`.
2. The tool takes up to 16,000 characters of diff. If the diff is longer, review it in parts rather than truncating silently — and say that you split it.
3. Call `jev_review_diff` on the `qualixar-jev` MCP server with:
   - `workspace_path`: the absolute path of the current workspace;
   - `goal`: the review goal, up to 1,000 characters;
   - `diff`: the diff text;
   - `data_classification`: the value this workspace's grant accepts, as stated in the Jev session guidance or by `jev_auto_status`. Under a Jev + Laya grant, pass `restricted` for a diff of private or client code, so Laya reviews it on this Mac and nothing is sent to Jev.

**This never approves code and never runs Git for you.** It suggests focus and risk. Do the actual review yourself against what it flags, and report anything it missed that you found — that gap is the useful signal. On an error, continue the review without Jev and say so in one line.
