from __future__ import annotations

from typing import Any

from .base import DataSource, SourceError
from .prometheus import PrometheusSource
from .sql import SQLSource

SOURCE_TYPES: dict[str, type[DataSource]] = {
    "sql": SQLSource,
    "zetaris": SQLSource,  # Zetaris exposes a SQL endpoint; same adapter, separate name for clarity
    "postgres": SQLSource,
    "prometheus": PrometheusSource,
}


def build_sources(cfg: dict[str, dict[str, Any]]) -> dict[str, DataSource]:
    sources = {}
    for name, scfg in cfg.items():
        kind = scfg.get("type", "sql")
        if kind not in SOURCE_TYPES:
            raise SourceError(f"source '{name}': unknown type '{kind}' (known: {', '.join(SOURCE_TYPES)})")
        sources[name] = SOURCE_TYPES[kind](name, scfg)
    return sources


__all__ = ["DataSource", "SourceError", "build_sources", "SOURCE_TYPES"]
