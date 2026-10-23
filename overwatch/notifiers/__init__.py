from __future__ import annotations

from typing import Any

from .base import Notifier, render_text
from .channels import NOTIFIER_TYPES, NotifyError


def build_notifiers(cfgs: list[dict[str, Any]]) -> list[Notifier]:
    out = []
    for cfg in cfgs:
        kind = cfg.get("type")
        if kind not in NOTIFIER_TYPES:
            raise ValueError(f"unknown notifier type '{kind}' (known: {', '.join(NOTIFIER_TYPES)})")
        if cfg.get("enabled", True):
            out.append(NOTIFIER_TYPES[kind](cfg))
    return out


__all__ = ["Notifier", "NotifyError", "build_notifiers", "render_text"]
