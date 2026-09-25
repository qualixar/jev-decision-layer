---
description: Open the private local setup wizard to enrol this workspace for Jev decisions
argument-hint: '[workspace path, defaults to the current directory]'
allowed-tools: Bash(pwd), AskUserQuestion
---

Enrol this workspace in the Qualixar Jev Decision Layer.

Arguments: `$ARGUMENTS`

1. Resolve the workspace path. Use `$ARGUMENTS` if given, otherwise the current working directory.
2. Call the `jev_auto_status` tool from the `qualixar-jev` MCP server for that path first. If it is already enrolled, report the current scope and stop — do not re-run setup unless the user asked to change it.
3. If it is not enrolled, call `jev_setup` with the exact workspace path. This opens a private two-step browser wizard.

**Never ask the user for a provider API key in chat, and never accept one in a tool argument.** The key is entered only in the local wizard, and is stored in the OS keychain. If the user pastes a key into the conversation, tell them it must go in the wizard instead and do not repeat it back.

Report what the wizard is for and wait — do not claim enrolment succeeded until `jev_auto_status` confirms it.
