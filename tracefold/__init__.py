"""Tracefold Market Research System: News V3."""

# DSPy 3.4 installs lazy anyio and openai proxies. Initialize the real modules
# before Tracefold imports DSPy: FastAPI needs anyio.abc, and LiteLLM imports
# openai._models during its first model call.
import anyio as anyio
import openai as openai
