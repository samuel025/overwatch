from __future__ import annotations

from typing import Any

from .base import DataSource, SourceError
from .docker import DockerSource
from .kubernetes import KubernetesSource
from .loki import LokiSource
from .prometheus import PrometheusSource
from .sql import SQLSource
from .zetaris import ZetarisSource

SOURCE_TYPES: dict[str, type[DataSource]] = {
    "sql": SQLSource,
    "zetaris": ZetarisSource,  # the data layer: JDBC with the Zetaris driver, Spark SQL
    "postgres": SQLSource,
    "prometheus": PrometheusSource,
    "docker": DockerSource,
    "kubernetes": KubernetesSource,
    "loki": LokiSource,
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
