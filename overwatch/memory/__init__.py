from __future__ import annotations

from typing import Any

from .base import Memory
from .hmem import HMEMMemory
from .local import LocalMemory, NoMemory


def build_memory(cfg: dict[str, Any]) -> Memory:
    kind = cfg.get("type", "hmem")
    if kind == "hmem":
        return HMEMMemory(cfg)
    if kind == "local":
        return LocalMemory(cfg)
    if kind == "none":
        return NoMemory()
    raise ValueError(f"unknown memory type '{kind}' (hmem, local, none)")


__all__ = ["Memory", "build_memory"]
