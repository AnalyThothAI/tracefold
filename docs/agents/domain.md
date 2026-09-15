# Domain exploration

News and Trading are sibling bounded contexts, composed by `tracefold.app`.
They are not one undifferentiated business module. The optional Nautilus process
executes Trading signals but does not give News or the Signal lane order authority.

For a business change, find the owning code and trace the relevant path from input
to persisted fact, decision, and actual consumer. Distinguish editorial News,
market observations, wallet net-buy episodes, and Trading execution; their admission,
retry, freshness, and completion meanings are not interchangeable.

Use [Architecture](../ARCHITECTURE.md#package-map) for package boundaries.
Ordinary cross-package consumers use the business package's public interfaces;
App composition and concrete integrations may use the explicit internal owners
allowed by the architecture tests. Do not expand public exports merely to wire an
internal implementation, or introduce an interface for every private helper.

Use persisted and public contract names consistently. [CONTEXT.md](../../CONTEXT.md)
clarifies review terminology: a model Proposal is not accepted Gold, and an AI
reviewer is not a human reviewer. Consult relevant historical decisions when needed,
not as an obligatory reading list. Record a material domain decision in the current
Issue or PR; no separate naming ticket or new documentation hierarchy is required.
