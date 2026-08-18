"""Control-plane commands: pause/resume/mute/unmute (pure parsing + evaluation; the CLI writes news_control_state)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

CONTROL_ACTIONS = frozenset({"pause_delivery", "resume_delivery", "mute_theme", "mute_symbol", "unmute"})
DEFAULT_MUTE_TTL_MS = 6 * 3600_000


@dataclass(frozen=True, slots=True)
class ControlCommand:
    action: str
    key: str | None
    ttl_ms: int


def parse_control(payload: Mapping[str, Any]) -> ControlCommand:
    action = str(payload.get("action") or "")
    if action not in CONTROL_ACTIONS:
        raise ValueError("news_control_action_invalid")
    key = payload.get("key")
    ttl_ms = int(payload.get("ttl_ms") or DEFAULT_MUTE_TTL_MS)
    if action in {"mute_theme", "mute_symbol", "unmute"} and not key:
        raise ValueError("news_control_key_required")
    return ControlCommand(
        action=action,
        key=str(key).strip() if key else None,
        ttl_ms=max(60_000, min(ttl_ms, 7 * 24 * 3600_000)),
    )


def apply_control(state: Mapping[str, Any], command: ControlCommand, *, now_ms: int) -> dict[str, Any]:
    paused = bool(state.get("paused"))
    mutes = [dict(m) for m in (state.get("mutes") or []) if int(m.get("until_ms") or 0) > now_ms]
    if command.action == "pause_delivery":
        paused = True
    elif command.action == "resume_delivery":
        paused = False
    elif command.action in {"mute_theme", "mute_symbol"}:
        kind = "theme" if command.action == "mute_theme" else "symbol"
        key = (command.key or "").upper() if kind == "symbol" else (command.key or "")
        mutes = [m for m in mutes if not (m.get("kind") == kind and m.get("key") == key)]
        mutes.append({"kind": kind, "key": key, "until_ms": now_ms + command.ttl_ms})
    elif command.action == "unmute":
        mutes = [m for m in mutes if m.get("key") not in {command.key, (command.key or "").upper()}]
    return {"paused": paused, "mutes": mutes}


def is_muted(state: Mapping[str, Any], *, storyline_key: str, grounded_assets: Sequence[str], now_ms: int) -> bool:
    theme = storyline_key.split(":", 1)[1] if ":" in storyline_key else storyline_key
    symbols = {a.upper().replace("XYZ-", "") for a in grounded_assets}
    for mute in state.get("mutes") or []:
        if int(mute.get("until_ms") or 0) <= now_ms:
            continue
        if mute.get("kind") == "theme" and mute.get("key") in {theme, storyline_key}:
            return True
        if mute.get("kind") == "symbol" and str(mute.get("key") or "").upper() in symbols:
            return True
    return False


__all__ = ["CONTROL_ACTIONS", "DEFAULT_MUTE_TTL_MS", "ControlCommand", "apply_control", "is_muted", "parse_control"]
