# Changelog

All notable changes to this project are recorded here. This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

No release states measured token, cost or time savings, because none has been measured.

## [1.0.1] — 2026-09-26

### Added

- **Offline fixtures.** Every recipe now ships three synthetic cases — nominal, uncertain and adversarial — in `fixtures/<recipe-id>.json`. Replaying all 96 runs the real gate with no provider call, no key and no workspace enrolment, so the layer can be checked before anything is spent on it. Available as `jev_recipe_selftest` or `plugins/qualixar-jev-decision-layer/scripts/jev selftest`.
- **VS Code host support.** VS Code exposes no hook surface, so its adapter registers the same stdio launcher the other hosts use as a workspace MCP server in `.vscode/mcp.json`. An existing file is merged — one entry added or updated, every other key kept — and one that does not parse is refused rather than overwritten. Plan mode is the default; writing takes an explicit flag. Available as `/jev-vscode` or `plugins/qualixar-jev-decision-layer/scripts/jev vscode`.
- **`jev-use-cases` skill** covering the twenty decision contracts: when to reach for one, the catalog → describe → fixture → evaluate sequence, and how to read a gated answer.
- **Two commands**, `/jev-selftest` and `/jev-vscode`, bringing the total to seven.
- **`docs/HOSTS.md`** — per-harness adapters, install, verified evidence against stated boundaries, and the checklist for adding a sixth harness.
- **`offline_fixtures`** in the capability manifest; manifest schema moves to version 2.

### Fixed

- **A verified-clean answer no longer costs a review.** Five recipes returned `request_review` in both directions: a document with no drift, a listing with no conflict, and copy that already matched the style guide all sent the host to a review it did not need, discarding the judgment the decision model had just settled. They now report a passed check. A catalog-wide test refuses any gate whose two outcomes are identical.

### Changed

- Documentation is host-neutral throughout. Getting started, rollback, security, use cases and the contributor guide no longer read as single-host documents, and per-host mechanics moved to the host guide.
- The host inventory reports a shipped adapter for VS Code. This states what the package contains and remains separate from conformance, which is still `NOT_RUN` for every host.
- The capability manifest corrects the Claude Code and VS Code rows, which claimed no installed adapter.

### Known limitations

- Plugin-provided MCP servers do not load in the Claude desktop app's **Code tab**. This affects every plugin on that surface, not this one: enabled plugins shipping MCP servers are equally absent there, with no error and no failed entry. Commands and skills load normally. Register the launcher directly for that surface — see the install notes.
- No live Copilot agent-mode turn has been run for VS Code. Registration is the whole integration; no extension ships.
- Recipe thresholds remain `UNVALIDATED_DEMONSTRATION_DEFAULT` and every recipe remains `SPECIFICATION_NOT_MODEL_EVALUATED`. A passing fixture run is a contract check on the local gate, never evidence of provider accuracy.
- Internal adapter, protocol and policy schema versions are contracts with data already on disk and are deliberately unchanged by this release.

## [1.0.0] — 2026-09-25

Initial public release: twenty reviewed decision contracts, thirty-two recipe specifications, typed Choice/Score/Noul answers with local receipts, five decision modes, the optional local Laya route (off by default), and host adapters for Codex, Claude Code, Antigravity and Hermes.
