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
