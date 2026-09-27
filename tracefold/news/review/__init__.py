"""Human truth acquisition: the ReviewDesk queue, its evidence views and its append-only submissions.

This plane runs against production, holds a database session, and is operated through the CLI. A review is a
person's judgment of what News actually sent; nothing here calls a model, optimizes, registers, arms or
promotes anything.
"""
