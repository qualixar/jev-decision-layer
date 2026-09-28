# GitHub repository discovery

Use this page to record GitHub repository metadata and the social-preview specification for `qualixar/jev-decision-layer`. This file does not change the live topics or preview image.

## Repository description

**Current verified repository description (checked 2026-09-28):**

> Typed decision layer for Codex, Claude Code, VS Code, Hermes and Antigravity. TypeSafe Jev or optional local Laya with policy gates, receipts and 36 recipes.

This is the verified live repository description as of 2026-09-28; it reflects the 1.0.7 public repository state. The 1.0.8 release candidate adds two recipes, so update the GitHub description and social preview only after the candidate passes its release gates. Keep the canonical repository URL stable.

## Suggested 1.0.8 description

> Typed decision layer for Codex, Claude Code, VS Code, Hermes and Antigravity. TypeSafe Jev or optional local Laya; local policy gates, receipts and 38 recipes.

Use this as a metadata update after the 1.0.8 candidate is verified. GitHub description text is a discovery aid, not a ranking or star guarantee.

## Topics

Suggested GitHub topics (20):

`jev` · `typesafe-ai` · `system-one` · `typed-decisions` · `decision-model` · `coding-agents` · `ai-agents` · `agent-routing` · `tool-selection` · `laya` · `laya-mlx` · `local-ai` · `apple-silicon` · `mcp` · `model-context-protocol` · `claude-code` · `openai-codex` · `vscode` · `hermes` · `antigravity`

Topics help classify a repository; they do not guarantee search placement or stars. See GitHub's [repository topics guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).

## Social preview

Upload [`docs/assets/social-preview.png`](assets/social-preview.png), the checked-in **1280 × 640 PNG**. Before using it, inspect the rendered file and confirm its wording and palette match these constraints:

- Headline: **ONE DECISION LAYER. FIVE AGENT HARNESSES.**
- Supporting line: **TypeSafe Jev · optional local Laya · typed answers · local gates · receipts**
- Count line: **20 tools · 38 recipes · 114 offline fixtures**
- Use a developer-focused violet, warm amber, and neutral palette. Do not use Qualixar green or cyan.
- Do not imply that all five hosts have verified live turns. Host evidence differs and is listed in the README.
- Keep execution authority with the host; do not depict Jev or Laya executing shell, browser, deployment, or publishing actions.

GitHub describes the [social preview image setting](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview). The current image should be visually checked before a metadata update; a filename or dimensions alone do not establish that the artwork meets this specification.

## README vocabulary

Use these terms naturally in the title, opening, headings, and explanatory text where they fit:

- TypeSafe Jev and System One;
- typed decisions and decision layer;
- coding agents and agent harnesses;
- Codex, Claude Code, VS Code, Hermes, and Antigravity;
- Laya and Laya-MLX;
- MCP, tool selection, retry decisions, context selection, and output relevance;
- local policy gate, `act / verify / ignore`, workspace scope, and receipts.

The first screen should show one bounded decision, explain what the layer adds beyond a direct Jev call, and state that host permissions remain authoritative. Link the host evidence table beside any five-host claim. Do not lead with the earlier Laya-MLX threshold sample: its labeled rows and reproduction script are absent from this repository, so it cannot justify a provider-specific gate floor.

## Working article title

**Jev + Laya for Coding Agents: Why We Built One Typed Decision Layer Across Five Harnesses**

This is a working title for the canonical educational article, not confirmation that the article has been published.

## Machine-readable discovery

Keep [`llms.txt`](../llms.txt) as a reading index for agents. It is not a ranking guarantee. When README headings or document locations change, check its links; it currently points to the README, host guide, capabilities manifest, first-use guide, use cases, security boundary, rollback guide, changelog, contributor guide, license, and third-party notices.

## Claim boundaries

- Say **one shared runtime with adapters for five hosts**; do not say all five are equally verified.
- Describe shipped thresholds as unvalidated defaults; do not imply provider calibration or an independently measured confidence signal.
- Treat 114 offline fixtures as tests of the shipped local gate contract, not provider-accuracy evidence.
- Do not claim token, subscription-cost, API-cost, or task-time savings without matched accepted-task trials.
- Say the model returns advice and local policy metadata; the host retains execution authority.
- Keep upstream model ownership and license notices separate; this is an independent integration, not an official TypeSafe, Laya, OpenAI, Anthropic, Google, or Microsoft product.
