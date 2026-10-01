"""Conflicts and lost ownership reported by News persistence commands."""


class EventUpdateConflict(ValueError):
    """An insert-only fact disagrees with the offered value for the same identity."""


class IntentLeaseLost(RuntimeError):
    """The caller no longer owns the notification intent it is writing under."""


class SemanticLeaseLost(RuntimeError):
    """The semantic attempt no longer owns its frozen input's work."""
