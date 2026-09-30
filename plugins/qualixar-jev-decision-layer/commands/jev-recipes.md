---
description: List the bounded Jev decision recipes, or run one against an input
argument-hint: '[recipe id and input, or blank to list all]'
allowed-tools: Bash(pwd), Read
---

Work with the data-only Jev recipe catalog.

Arguments: `$ARGUMENTS`

- **No arguments** — call `jev_recipe_catalog` on the `qualixar-jev` MCP server and list the recipes grouped by domain, with what decision each one makes, its input fields, and, for a recipe with fixed options, those options.
- **A recipe id plus an input** — read that recipe's input fields from `jev_recipe_catalog` first. Then call `jev_recipe_try` with:
  - `workspace_path`: the absolute path of the current workspace;
  - `recipe_id`;
  - `input`: an object with exactly the recipe's fields, no more and no fewer (anything else is refused as `RECIPE_INPUT_INVALID`);
  - `data_classification`: the value this workspace's grant accepts, as stated in the Jev session guidance or by `jev_auto_status`. Under a Jev + Laya grant, pass `restricted` when the input is private or client content, so Laya decides it on this Mac and nothing is sent to Jev.

A recipe with fixed options (for example lead routing, support triage or content format) chooses only among its own options. If the user's categories have other names, use `jev_route` with the user's own list instead.

Recipes are **specifications, not accuracy evidence**. A recipe existing does not mean it has been measured on your data. In this release every live result is `verify` or `ignore`, so report `recommendation`, `confidence` and `reasons`, treat the answer as experimental advice, and say so.
