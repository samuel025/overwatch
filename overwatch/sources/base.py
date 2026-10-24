"""DataSource interface. Add a new integration by implementing `query`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SourceError(Exception):
    pass


class DataSource(ABC):
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.max_rows = int(cfg.get("max_rows", 50))

    @property
    @abstractmethod
    def language(self) -> str:
        """Query language the model should write for this source, e.g. 'SQL' or 'PromQL'."""

    @abstractmethod
    def query(self, q: str) -> list[dict[str, Any]]:
        """Run a read-only query and return at most `max_rows` rows."""

    def close(self) -> None:
        pass


def duration_text(delta) -> str:
    """A duration as plain text with a unit. Python's own str() writes a tiny negative interval as
    "-1 day, 23:59:59.996", which a reader (or a model) takes for a day."""
    seconds = delta.total_seconds()
    mag = abs(seconds)
    if mag < 120:
        return f"{seconds:.2f} s"
    if mag < 7200:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"
