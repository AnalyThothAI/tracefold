# Documentation

This is the entry point for the maintained handbook. The module guides were
reviewed against **main `a1f4a9ac1ae8795be93b644eb5e4b88e0bce919b`** on
2026-09-27. Relative source links resolve in the same checkout as these documents.
This records a source review, not the deployed image or an account-health claim.

**Current branch boundary:** PR #711 merged while this handbook was being prepared.
The guides were rebased and updated against the resulting main commit above.
News now produces versioned EventUpdates; its old three-predictor Program,
GEPA/release/canary execution and four-axis taxonomy are not current capabilities.
The [EventUpdate reference](design/news-event-updates.md) records the detailed
identity/cutover contract. This is source documentation, not deployment verification.

## Start with a question

| Question | Maintained owner |
| --- | --- |
| What is this project, and how do I start it? | [Repository README](../README.md), [Setup](SETUP.md) |
| Which processes run, and where is the durable truth? | [Architecture](ARCHITECTURE.md) |
| What happens to one news item? Which steps call a model? | [News](modules/news.md) |
| What does OI measure, and why did it notify or not trade? | [OI and market observations](modules/oi.md) |
| How does an input become a Case, WATCH or Signal? | [Trading Analysis](modules/trading.md) |
| Who can place orders, and what proves an execution? | [Execution](modules/execution.md) |
| How do several wallets produce one net-buy alert? | [Wallets](modules/wallets.md) |
| Which review/calibration tools remain after the EventUpdate cut? | [Review and calibration](modules/learning.md) |
| Where are adapters, configuration and database boundaries? | [Platform and integrations](modules/platform.md) |
| How does the read-only console consume these facts? | [Frontend](FRONTEND.md) |
| Where is a particular file or public contract? | [Repository map](generated/repository-map.md), [Contracts](CONTRACTS.md) |
| How do I diagnose, upgrade or restore? | [Operations](OPERATIONS.md), [Migrations](MIGRATIONS.md), [Security](SECURITY.md) |
| How do I change and test the implementation? | [Development](DEVELOPMENT.md), [Testing](TESTING.md) |

## Reference and evidence

[Generated references](generated/README.md) own exact CLI, HTTP, schema and file
inventories. [News topics and source authority](NEWS_TAXONOMY.md) owns the retained topic codebook
and source-authority meaning; [CONTEXT.md](../CONTEXT.md) owns review language and acceptance context.

[Research records](research/README.md) and [engineering receipts](reports/README.md)
are explicitly historical evidence, not installation instructions or additional
runtime requirements. [Research notebooks](../notebooks/README.md) retain their
recorded inputs and limitations. The [execution ownership ADR](adr/0002-trading-execution-owner-hard-cuts.md)
explains old terms found in backups. The [wallet cutover](wallet-net-buy-cutover.md)
is a version-specific migration procedure, not the normal startup path.

Coding-agent guidance is routed by [shared-router](agents/shared-router.md),
[domain exploration](agents/domain.md), [worktrees](agents/worktrees.md),
[issue/PR scope](agents/issue-tracker.md), and [triage labels](agents/triage-labels.md).
Those pages describe repository work, not another business architecture.

## Keeping the handbook current

Each concern has one owner: README is the front door; Architecture owns system
boundaries; module pages own behavior; Setup/Operations own commands;
Contracts/generated references own exact shapes; history owns past evidence.
Link to another owner instead of copying its full policy or a changing list of fields.

For a changed module, update its entry points, diagram, state/failure explanation
and test links together. Use Mermaid fences in Markdown for maintained diagrams;
keep diagrams small and name real processes, contracts and writers. A sequence
step is not automatically a persisted state. Do not label a proposal, test result
or historical replay as a deployed capability.

Delete superseded design proposals when the implemented behavior has a maintained
owner. Retain a historical record only when it carries useful decision provenance,
measurements, reproducible inputs or a required cutover. Being old or having no
inbound Markdown link alone is not enough to delete evidence. Git history retains
removed proposals without a second archive directory in the current handbook.

After editing, run the local-link and repository-map checks described in
[Generated references](generated/README.md), then the focused documentation tests.
Generated inventories must be regenerated, not hand-edited. Mermaid rendering is
checked separately from the hermetic Python checks; those checks do not prove the
diagrams render or that an operator deployment succeeded.
