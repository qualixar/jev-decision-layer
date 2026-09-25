---
description: List the bounded Jev decision recipes, or run one against an input
argument-hint: '[recipe id and input, or blank to list all]'
allowed-tools: Bash(pwd), Read
---

Work with the data-only Jev recipe catalog.

Arguments: `$ARGUMENTS`

- **No arguments** — call `jev_recipe_catalog` on the `qualixar-jev` MCP server and list the recipes grouped by domain, with what decision each one makes.
- **A recipe id plus an input** — call `jev_recipe_try` with the workspace path, the recipe id, and the input object.

Recipes are **specifications, not accuracy evidence**. A recipe existing does not mean it has been measured on your data. Treat the answer as experimental advice and say so when reporting it.
