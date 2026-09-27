# Handbook

These documents describe the implementation in the same checkout. Read the owning
module and its tests for exact behavior; the deployed image may differ. Each concern
has one maintained page. Closed issue plans and historical reports are not additional
runtime rules.

## Find the owner

| Question | Document |
| --- | --- |
| What does Tracefold do? | [Repository README](../README.md) |
| How do I start/configure it? | [Setup](SETUP.md) |
| Which processes, packages and data stores own the work? | [Architecture](ARCHITECTURE.md) |
| How does a source revision become an EventUpdate and notification? | [News](modules/news.md) |
| What does OI mean, and how are market observations processed? | [OI and market observations](modules/oi.md) |
| How does an input become a Case, WATCH or Signal? | [Trading Analysis](modules/trading.md) |
| What actually places orders and reconciles fills? | [Execution](modules/execution.md) |
| How do wallet receipts become a concentrated net-buy alert? | [Wallets](modules/wallets.md) |
| Which review and calibration capabilities currently exist? | [Review](modules/review.md) |
| Where are infrastructure, adapters and composition boundaries? | [Platform](modules/platform.md) |
| How does the read-only console use the data? | [Frontend](FRONTEND.md) |
| What are the public and generated contracts? | [Contracts](CONTRACTS.md), [generated references](generated/README.md) |
| How do I diagnose and recover current work? | [Operations](OPERATIONS.md) |
| How do I change or restore a schema safely? | [Migrations](MIGRATIONS.md) |
| Who holds credentials and write authority? | [Security](SECURITY.md) |
| How do I develop and verify changes? | [Development](DEVELOPMENT.md), [Testing](TESTING.md) |

The News guide includes the current topic/source-authority model and the complete
EventUpdate identity/recovery contract. There is no second taxonomy manual or
parallel EventUpdate design document to reconcile with it.

## Repository work and offline research

[Shared agent routing](agents/shared-router.md), [domain exploration](agents/domain.md),
[worktrees](agents/worktrees.md), [issue/PR scope](agents/issue-tracker.md), and
[triage labels](agents/triage-labels.md) describe repository work, not business policy.
[CONTEXT.md](../CONTEXT.md) defines honest review terminology.

[Notebooks](../notebooks/README.md) identifies current offline utilities versus
historical experiments and preserved inputs. Historical studies, one-off query
plans, rollout transcripts and superseded architecture proposals are retrieved
from their original Git revision when needed. They are not kept beside current
instructions merely with an “outdated” banner. Frozen datasets are not rewritten
to update historical prose references.

## Maintain a change once

Update the owning page with the implementation: source entry points, input/output,
state ownership, failure/retry behavior and test links. README stays the front door;
Architecture stays the system map; module pages explain behavior; Operations and
Migrations own actions; schemas/help own exact syntax. Link rather than copy.

Use small Mermaid diagrams for processes, data flow, state or timing. A processing
step is not automatically a database state, and two different persisted state
axes must not become one overall success badge. Render changed diagrams before
submission; local-link checks alone do not establish a successful rendering.

Run `python scripts/check_mandatory_docs_links.py` and the focused documentation
checks in [Development](DEVELOPMENT.md). The check covers local files, Markdown
anchors and reference links. It does not prove remote link availability, model
quality or a running deployment. Documentation work does not require a new service,
a documentation site or a generated index of every source declaration.
