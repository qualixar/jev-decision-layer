---
description: Register this layer as a workspace MCP server for VS Code Copilot agent mode
argument-hint: '[workspace path, defaults to the current directory]'
allowed-tools: Bash(pwd), Bash(*/scripts/launch-jev:*), Read
---

Set up the VS Code host adapter for the Qualixar Jev Decision Layer.

Arguments: `$ARGUMENTS`

VS Code exposes no hook surface, so its adapter registers the same stdio launcher the other hosts use as a workspace MCP server in `.vscode/mcp.json`, which Copilot agent mode reads.

1. Run the adapter in plan mode first, against `$ARGUMENTS` or the current directory:

   `python3 -m jev_auto.cli vscode --workspace <path>`

   This writes nothing. Show the user the `action`, the `config_path`, and `preserved_servers` — the list of their existing servers that will be kept untouched.

2. Only after the user agrees, re-run with `--write`.

A file that already exists is merged, never replaced: exactly one `qualixar-jev` entry is added or updated and every other key is carried through. If the adapter reports `VSCODE_CONFIG_UNPARSEABLE`, stop and tell the user — it refuses to rewrite a file it cannot read rather than discarding settings it cannot see. Do not hand-edit the file to work around this.

VS Code keys servers under a top-level `servers` object, not `mcpServers`. Do not "correct" it to match another host's format; the wrong key produces no error and no server.

Restart VS Code after writing, then confirm the server appears in its MCP list.
