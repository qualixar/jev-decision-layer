# Changelog

All notable changes to this project are recorded here. This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.10] — 2026-09-29

### Fixed

- **Session-start failures are no longer silent.** When a workspace is enrolled but the local broker cannot start, the Codex hook names the reason on stderr (`qualixar-jev: local service unavailable (<code>)`) and continues without guidance. Exit stays 0; unenrolled workspaces stay fully silent. The failure that motivated this was a half-replaced 1.0.8 hook cache during a mid-session marketplace upgrade, which no in-repo code can survive; this change makes the remaining failure class, an enrolled workspace with an unreachable broker, diagnosable instead of invisible.
- **No consent or routing changes.** Counts stay 20 tools, 38 recipes, 114 fixtures. Supported and verified on macOS; Linux experimental; Windows hosted runtime disabled.

## [1.0.9] — 2026-09-29

### Added

- **Hierarchical workspace enrollment.** A reviewed root can cover child directories and nested repositories. The setup wizard asks for that coverage separately, and `jev enroll --cover-descendants` requires the additional `COVER CHILDREN` confirmation. An exact child policy wins. Revoking a covered child records a local refusal and leaves the root grant in place. Home directories and filesystem tops cannot be descendant roots. Codex, Claude Code, Hermes, Antigravity, and VS Code resolve that consent through one binding, and the broker and budget stay on the grant root.
- **Host enrollment parity tests** for those five adapters. `native_status` remains `NOT_RUN`; an in-process adapter test is not a native host session.

### Unchanged

- **20 MCP tools, 38 recipes, and 114 synthetic offline fixtures.** Published launch figures stay as recorded.
- **Platform scope.** 1.0.9 is supported and verified on macOS. Linux remains experimental and unverified. Windows hosted runtime entry points stay closed. Local Laya-MLX stays on supported Apple-Silicon Macs.

## [1.0.8] — 2026-09-29

This release strengthens the live recipe gate, adds a local workbench, and gives managers and creators two more bounded decision recipes. Platform and host evidence is recorded in [the host guide](docs/HOSTS.md).

### Added

- **Two audience-specific recipes**, taking the source catalog to 38 recipes and 114 synthetic fixtures: `qualixar.work-item-priority` helps a manager compare one item against an explicit rubric and supplied priorities; `qualixar.content-repurpose` selects among creator-provided content formats. Both return advice for a human or host to review.
- **A local recipe workbench** with form-based recipe inputs, offline synthetic examples, and a separately reviewed live path. It runs on loopback and does not execute recommendations.
- **Supported platform scope:** 1.0.8 is supported and verified on macOS. Linux remains experimental and unverified. Windows hosted runtime entry points fail closed because the native private-state contract did not pass CI; the Windows CI lane has been removed until that contract is deliberately revalidated. Local Laya-MLX stays Apple-Silicon macOS only.

### Changed

- **Live recipe answers are now evaluated by the packaged local policy gate.** `jev_recipe_try` returns the gate outcome, `host_action`, and a `policy_receipt_id` linked to the provider receipt. The response is advisory and sets `execution_authorized` to false.
- **Every would-be passing recipe answer is capped to `VERIFY`** while its status is `SPECIFICATION_NOT_MODEL_EVALUATED`. An explicit `unknown` can return `IGNORE`; malformed or below-threshold answers remain `VERIFY`. This does not establish provider accuracy.
- **Score gates now check probability mass on the side of the configured score threshold**, rather than accepting an unrelated distribution summary.
- **Choice gates now reject truncated probability distributions**, including missing alternatives or a total mass outside a small rounding tolerance. The Laya-specific confidence-floor override and the cross-field confidence-shortfall rule from 1.0.7 were removed: the repository does not ship the labeled evidence needed to justify that override, and TypeSafe defines confidence as a statistic derived from the same distribution. Every provider now uses the recipe's unvalidated floor; no profile claims calibrated thresholds.
- **Host adapter documentation distinguishes each native surface and its evidence.** Codex hook configuration is separate from Claude hook configuration; Antigravity remains PreInvocation advisory only; Hermes exposes its explicit tool allow-list; VS Code registers an MCP server. The adapters do not claim equal native verification.
- **Hermes now registers `jev_verify` and `jev_rerank` and accepts validated Windows local-drive workspace paths.** The earlier 1.0.4 changelog entry described those tools as fixed, but the shipped Hermes allow-list and manifest still omitted them; this candidate repairs that actual installed surface and tests schema parity.

### Platform and host notes

- Linux hosted support remains experimental until native Secret Service, broker, host, and provider integration is verified. Windows hosted support remains disabled until native private-state and broker contracts pass CI.
- Native model/tool turns remain host-specific evidence; a staged manifest, plugin validator, or MCP handshake alone is not a live-turn verification.

## [1.0.7] — 2026-09-26

### Added

- **Provider-specific threshold override and a confidence-shortfall rule** were introduced in this release. This entry is retained to describe the historical code, not to endorse its evidence: a later audit found only ten unlabeled probability/confidence pairs in the repository, rather than a reproducible labeled set for the earlier fourteen-sample and correctness claims. It also found that TypeSafe computes confidence from the reported distribution, so treating their difference as two independent signals was wrong. Both rules are removed in the 1.0.8 candidate above.

- **Nine defects found while raising test coverage**, each reproduced before being fixed: a bare `ValueError` escaping the typed-error contract on a malformed workspace path; a config write that crashed instead of refusing when its directory was unwritable; a recipe fallback that could never run because the module it imported has never existed; falsy rubric values silently replaced by boilerplate, so the model read a different question than the author wrote; a size check that a file could grow past between measurement and read; a credential store that wrote a key file it then refused to read back; a type guard that ran after the code it guarded; `--state-base` ignored by every typed tool; and `UNKNOWN_CASE` masked as an internal broker error.

- **A coverage floor** (`tools/coverage_gate.py`) recorded per module and never lowered automatically, plus guards that every shipped runtime file is named in the tamper manifest and that no host-facing file ships unreferenced.

### Fixed

- **The portable package still handed Codex a Claude variable.** 1.0.6 repaired the generated Codex package; the portable package's Codex overlay still pointed at the Claude connection descriptor, so installing it into Codex failed exactly as before — a command containing `${CLAUDE_PLUGIN_ROOT}`, which Codex does not expand. Both packages now name one Codex descriptor, so the shared overlay cannot point at the wrong host's file.

- **The server reported 1.0.0 from every release since 1.0.0.** The runtime stated its release in a module docstring and in a value beside it, and only the docstring was ever updated. A host asking which version answered — the fastest way to find a package whose server never started — was told 1.0.0 whatever it had installed. Each package now states its release once and everything that reports a version reads it from there. A test starts both launchers and compares what they say against what shipped.

- **The Hermes manifest under-declared its own tools.** `plugin.yaml` listed nine while the adapter served twelve. The allow-list had been pinned against the served surface since 1.0.4, but nothing pinned the manifest an operator actually reads.

- **A Claude hook file shipped inside the Codex package**, and an MCP descriptor shipped that no manifest named. Neither was reachable, and both were the same shape as the defect above: a file a host could find and act on, belonging to a different host.

### Added

- **A test that fails when any host is handed something it cannot resolve.** It reads each package's manifests, follows what they point at, and checks every variable against the vocabulary that host expands, so this class of defect cannot return in a file nobody thought to check. Run against the 1.0.5 tree it names the original Codex failure in one line. Nothing host-facing may ship unreferenced, and every version literal must be declared as either product identity or a frozen contract — the broker handshake and the on-disk document versions are pinned as contracts and deliberately do not move with the release.

- **`tools/release.py`** sets the release across every declared site and regenerates the Codex package, the recipe catalog and the runtime manifest, printing each runtime file it re-seals. `--check` verifies without changing anything. Five host adapters cannot be version-bumped by hand, and the evidence that they cannot is the previous six releases.

## [1.0.6] — 2026-09-26

### Fixed

- The Codex package now has its own MCP connection descriptor instead of copying Claude's `${CLAUDE_PLUGIN_ROOT}` command. Both packages still launch the same bundled server. A package test starts each launcher and checks its offline tool list.
- MCP tool discovery no longer creates a legacy state directory before the host can list tools. A read-only or restricted host can discover the same twenty tools without a workspace grant or provider call.

### Upgrade

- Finish the active Codex task, fully quit Codex Desktop, refresh the marketplace and installed plugin, then reopen it. A running task may still hold a hook path from the previous version's removed cache; the [setup guide](docs/GETTING_STARTED.md) covers that recovery.

## [1.0.5] — 2026-09-26

### Fixed

- **`jev_rerank` scored each passage against its neighbours instead of the question.** Found by running it against a live provider, not by reading it. The instruction said "judge the passage on its own", but every passage sits in the shared state, and the model read them together anyway.

  Measured: *"The team agreed in Q2 to add a caching layer"*, asked "what invalidates a cache entry?", scored **0.73** in a set containing no answer and **2.72** in a set containing one. Identical passage, identical question. In the second set it ranked **first**, above the passage that actually answered, and all three came back `usable`.

  Each per-passage question now restates the question and names its own passage, so it stands alone, and the scale names the case that was scoring too high — background, history, configuration and "we decided to build it" are level 1, explicitly. Re-measured on the same two sets: the drift fell to **0.03**, the answering passage moved from last to **first at 2.95**, and `usable_count` fell from 3 to 1.

  The retrieval inversion is now stark: retrieval ranked the answering passage **last** at 0.62; this ranks it **first**.

  `should_abstain` was correct throughout and is unchanged — 0.07/0.08 on the unanswering set, 0.91 on the answering one. The set-level question was never the problem.

## [1.0.4] — 2026-09-26

### Fixed

- **Hermes could not reach `jev_verify` or `jev_rerank`.** Every other host spawns the same launcher and gets the whole tool surface automatically; Hermes filters through its own allow-list, and the two new tools were never added to it. They were reachable from four hosts and invisible from the fifth, with nothing failing. A test now pins that allow-list against the served surface, with a documented reason for each deliberate exclusion, so adding a tool forces a decision rather than relying on memory.

### Added

- The README states the shipped version, host count, tool count and fixture count, and a test checks each against what actually ships.

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
