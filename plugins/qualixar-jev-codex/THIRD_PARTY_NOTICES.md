# Third-party notices

Qualixar Jev Decision Layer is independently maintained. It does not redistribute TypeSafe Jev, Laya model weights, or any provider credential.

| Upstream | Use | License notice |
|---|---|---|
| [wy-coliney/jev-browser-use](https://github.com/wy-coliney/jev-browser-use) at `f14b60e0ae1ee90cd73eb6650e30a666a84c021a` | Adapted existing-tab accessibility parsing and bounded browser choice. The plugin uses private local IPC instead of forwarding provider credentials through the browser. | [MIT notice](licenses/jev-browser-use-MIT.txt) |
| [GhalebDweikat/winnow](https://github.com/GhalebDweikat/winnow) at `51d80b945c74c8384bc47fa817179f668289afd8` | Verbatim chunking component in `runtime/jev_auto/vendor` and independently adapted reversible selection. | [MIT notice](licenses/winnow-MIT.txt) |
| [mizorewww/laya-mlx](https://github.com/mizorewww/laya-mlx) at `0a859518634112655cb97c745dbf04f5191aaf13` | Optional separately installed local runtime; no MLX implementation or model weights are bundled. | Apache-2.0 notices remain with the separate installation. |

## Recipe definitions

Some recipes in `recipes/` adapt categories or rubric ideas from these projects. No upstream text is copied verbatim; every definition was condensed and reworded for a bounded decision, which is a modification. Each recipe's own limitations say where its wording came from.

| Upstream | Used by | License notice |
|---|---|---|
| [PagerDuty/incident-response-docs](https://github.com/PagerDuty/incident-response-docs) at `464fc9d3e47e19e9d8da17cec1a41dc09624e95a`, Copyright 2016 PagerDuty, Inc. | `qualixar.incident-severity`: the SEV-1 to SEV-5 intents and the advice to assume the more urgent level when unsure, condensed and reworded. | Apache License 2.0; [license text](licenses/Apache-2.0.txt) |
| [a11yproject/a11yproject.com](https://github.com/a11yproject/a11yproject.com) at `27de226845bb7729fd0c28a7652b3e47deae53b1`, The A11Y Project checklist | `qualixar.accessibility-gap-routing`: the checklist groups for images, captions and transcripts, colour, headings and links informed the labels; reworded. | Apache License 2.0; [license text](licenses/Apache-2.0.txt) |
| [ossf/scorecard](https://github.com/ossf/scorecard) at `bd19d486f4a378f65cf410915f69d7ac609dd25f`, Copyright 2020 OpenSSF Scorecard Authors | `qualixar.dependency-adoption-triage`: the check names Vulnerabilities, License, Maintained and Signed-Releases informed the review routes; no text copied. | Apache License 2.0; [license text](licenses/Apache-2.0.txt) |
| [explodinggradients/ragas](https://github.com/explodinggradients/ragas) at `298b68274234c060deacab3cf5fb52aa3a20e885`, Copyright 2023 Vibrant Labs | `qualixar.summary-faithfulness`: faithfulness as every claim being inferable from the given context; reworded. | Apache License 2.0; [license text](licenses/Apache-2.0.txt) |
| [braintrustdata/autoevals](https://github.com/braintrustdata/autoevals) at `9546b28d08a4c4f55994e0626e4c327a9caa5ec0` | `qualixar.summary-faithfulness` and `qualixar.translation-fidelity`: the factuality template's subset, superset and disagreement cases, and the translation template's check of meaning-changing words; reworded. | [MIT notice](licenses/autoevals-MIT.txt) |
| [olivierlacan/keep-a-changelog](https://github.com/olivierlacan/keep-a-changelog) at `08d0df5a7e93b71d902def8be0f0d40025b56289`, Keep a Changelog 1.1.0 | `qualixar.changelog-category`: the six change types Added, Changed, Deprecated, Removed, Fixed and Security; the precedence order is this project's own. | [MIT notice](licenses/keep-a-changelog-MIT.txt) |
| [mgreiler/code-review-checklist](https://github.com/mgreiler/code-review-checklist) at `35875e6383061cc83bccace1884c7dbce1262700` | `qualixar.review-comment-severity`: the review dimensions (logic errors, error handling, security and data privacy, tests) informed the blocking and should-fix definitions. | [MIT notice](licenses/code-review-checklist-MIT.txt) |
| [adr/madr](https://github.com/adr/madr) at `ba75bb1b20d42af5746b246ad348c202419ae681`, MADR | `qualixar.decision-record-completeness`: the record parts context and problem, considered options, decision outcome and consequences. | Dual-licensed MIT OR CC0-1.0; used under CC0-1.0, which requires no notice. Credited as a courtesy. |

These projects do not endorse this one.

The selective browser skill also takes design inspiration from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast): closed observed controls, one bounded decision over competing actions, and freshness checks. No `jev-ultrafast` source or browser runtime is bundled. Patterns studied from other Jev repositories are not copied code or endorsements. Original authors retain their copyrights. Jev belongs to TypeSafe AI; Laya and its runtime belong to their respective maintainers. This project is not an official product of those parties or OpenAI.
