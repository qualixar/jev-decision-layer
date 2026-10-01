# Jev Decision Layer: a bounded choice with evidence

Jev Decision Layer is an open-source decision layer for AI agents. It connects typed task, tool, skill and review choices to TypeSafe Jev, with local policy gates and auditable receipts. The host agent retains execution authority.

The first demonstration is deliberately inspectable: twenty public recipes, each replayed through nominal, uncertain and adversarial synthetic cases. The runnable artifact checks local gate behavior before a user connects a model. It does not claim faster accepted tasks or cheaper subscriptions.

This is one AI Reliability Engineering primitive: a decision model advises, a local gate checks the answer, and the host executes and verifies. Use it when the options or rubric can be stated clearly. Keep open-ended writing, code generation and final judgment with the host.

Start with the [offline demo](../DEMO.md), then read the [host guide](../HOSTS.md). macOS is supported; Linux is experimental and unverified; Windows is disabled. Hosted decisions require a reviewed scope and provider billing. Optional Laya runs on Apple Silicon after its installation is verified.

Qualixar is an independent research initiative by Varun Pratap Bhardwaj. This integration is not an official TypeSafe or host-provider product.

Product page: https://qualixar.com/products/jev-decision-layer

Source: https://github.com/qualixar/jev-decision-layer
