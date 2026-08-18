# Domain exploration

Tracefold is one bounded context. `docs/ARCHITECTURE.md` is the sole current
backend architecture map.

Before changing business behavior:

1. identify the owning root interface: `tracefold.news` or
   `tracefold.macro` (Macro's general market observation facts live under
   `tracefold.macro`; there is no separate market package);
2. trace provider input to PostgreSQL fact, durable target or broker queue,
   current row, and public consumer;
3. preserve the glossary embodied by persisted fact names and public
   contracts;
4. import another business capability only from its package root;
5. update the GitHub Issue when the accepted domain decision changes.

Optional root `CONTEXT.md`, `CONTEXT-MAP.md`, or ADR files may be consulted when
present. Their absence is not an error and does not justify creating a second
documentation hierarchy.

If an established term is insufficient, make the naming decision explicit in
the current issue before introducing a competing synonym.
