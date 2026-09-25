"""Tracefold Market Research System: News V3."""

# DSPy 3.4 installs a lazy anyio proxy. Initialize the real module before
# Tracefold imports DSPy so FastAPI can later import anyio.abc in any order.
import anyio as anyio
