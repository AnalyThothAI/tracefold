from __future__ import annotations

from typing import Any

CommandPayload = dict[str, Any]
CommandResult = int | tuple[int, CommandPayload]

__all__ = ["CommandPayload", "CommandResult"]
