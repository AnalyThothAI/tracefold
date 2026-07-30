from __future__ import annotations


class CpuTaskTimeout(TimeoutError):
    pass


class CpuTaskProcessExpired(RuntimeError):
    pass


__all__ = ["CpuTaskProcessExpired", "CpuTaskTimeout"]
