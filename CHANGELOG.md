# Changelog

All notable changes to this project are recorded here. This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

No release states measured token, cost or time savings, because none has been measured.

## [1.0.3] — 2026-09-26

### Added

**Verification and reranking now ship inside the product.** Both existed only in a private lab, which meant nobody but their author could install them. They are the two capabilities the layer was missing, and they are the two a host most often answers expensively in its own context.

- **`jev_verify`** — check a structured extraction against the source it claims to come from. One Noul per field in a single call, returning a per-field probability that the field is **wrong**. A populated field is asked whether the source contradicts it; an empty one whether the source actually states a value. Those are deliberately different questions: measured against jev-1.13, a single phrasing scored a correctly-empty field at p_wrong 0.98, because "absent from the source" is trivially true of an empty value.
- **`jev_rerank`** — score retrieved passages on an absolute scale and answer the question a retriever cannot: does this set contain the answer at all? A retrieval score ranks within a set, so the best of five irrelevant passages still ranks first. When `should_abstain` is true, the honest answer is "I do not have this", not the top hit.

**Absence of evidence is never a pass.** In both tools an unmeasured result is explicit. A field the model did not answer is `unknown`, never `ok`, and always blocks `trustworthy` — so a provider outage cannot look like a clean record. A passage with no score is not usable, and a set whose answer could not be measured abstains. Callers branch on `trustworthy` and `should_abstain`, never on an empty suspect list, which is also empty when nothing could be measured.

Both route through the same broker as every other tool: workspace consent, secret screening, daily budget, and a local receipt.

### Changed

- The catalog is now **20 tools**.
- The standalone lab MCP server is retired. One install, one server, everything included.

## [1.0.2] — 2026-09-26

### Fixed

An external audit of the 1.0.1 gate found, and this session reproduced, twelve ways a typed answer could reach `host_action: act` when nothing should have acted on it. `act` is the promise that the host need not think again, so every one of these was the most expensive failure the layer can produce. **The gate now fails closed in all of them.**

- **An unreadable threshold no longer drops the gate.** A policy with `min_confidence: "high"` silently discarded the floor and cleared a confidence of 0.1. A threshold that cannot be read is a broken gate, not an absent one.
- **Values outside their own domain are refused** — a Noul of 1.5 or −2, a confidence of 999 clearing a floor of 0.7, a probability of −0.5.
- **Self-contradictory answers are refused** — a label the model scored below another, a distribution holding more than one unit of mass, a label that is not a string.
- **`act` is never returned with nothing to act on**, and inverted Noul bands, malformed thresholds, a confidence floor on a Noul, and a gate whose two outcomes are identical are now rejected when the catalog loads rather than surfacing later as a bad decision.
- **`evaluate` no longer raises on content**, as its contract always claimed: an unhashable label or a non-dict policy returns a verdict instead of crashing.
- **The offline proof is no longer tautological.** A replay only compared the gate against the expectation recorded beside it, so an `uncertain` case that confidently cleared the gate — with `act` written next to it — reported a pass. Each variant's intent is now checked independently of what was recorded, and one broken case no longer aborts the other hundred and seven.
- **A plan no longer prints other servers' secrets.** Registering with a host returned the whole merged config, including every `env` block in it. Only our own entry is reported now, and a secret under our own name is masked.
- **Host config writes are atomic** and cannot be redirected by swapping the path between the symlink check and the write.

### Added

- **Four new decision surfaces**, taking the catalog to 36 recipes and 108 fixtures: `release-readiness` (does this evidence satisfy a release requirement), `retry-decision` (is this failure worth retrying, or is retrying waste), `disclosure-check` (does this message say more than the change requires), and `output-relevance` (is this tool result worth reading at all).
- **Antigravity MCP registration.** Antigravity supports a global `mcp_config.json`, which this package never shipped into. `jev host-register --host antigravity` now writes it with an absolute launcher path. A plugin-relative command is deliberately still not shipped: Antigravity does not document how it resolves one.
- **Hermes** can now reach `jev_recipe_selftest`, which is offline and workspace-free.
- **`host-register`** covers VS Code, Antigravity and the Claude desktop config from one table, because each keys and shapes its server entry differently and every wrong shape fails silently.

### Known limitations

- The Claude desktop app holds its config in memory and flushes it on exit, overwriting an external edit made while it is running. Register before starting it, or use its own settings UI.
- Everything listed under 1.0.1 still applies.

## [1.0.1] — 2026-09-26

### Added

- **Offline fixtures.** Every recipe now ships three synthetic cases — nominal, uncertain and adversarial — in `fixtures/<recipe-id>.json`. Replaying all of them runs the real gate with no provider call, no key and no workspace enrolment, so the layer can be checked before anything is spent on it. Available as `jev_recipe_selftest` or `plugins/qualixar-jev-decision-layer/scripts/jev selftest`.
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
