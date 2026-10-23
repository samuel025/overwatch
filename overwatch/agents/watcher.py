"""Watcher: evaluates the configured checks. Deterministic on purpose: cheap, fast, no model call.

A check can alert on a fixed threshold, relative to its own recent normal ("5x the usual
latency"), when it returns no data, or any combination.
"""

from __future__ import annotations

import operator
import statistics
import time
from collections import deque

from ..config import CheckConfig
from ..models import Anomaly
from ..sources import DataSource, SourceError

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
RECENT_SECONDS = 120  # samples newer than this are "now", not part of the baseline
MIN_BASELINE_SAMPLES = 10


def _first_number(rows: list[dict]) -> float | None:
    if not rows:
        return None
    row = rows[0]
    if "value" in row and row["value"] is not None:
        return float(row["value"])
    for v in row.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


class Watcher:
    def __init__(self, checks: list[CheckConfig], sources: dict[str, DataSource], history_size: int = 720):
        self.checks = checks
        self.sources = sources
        self.last_values: dict[str, float | None] = {}
        # check name -> (unix time, value) samples; feeds relative checks and the live charts
        self.history: dict[str, deque] = {c.name: deque(maxlen=history_size) for c in checks}

    def baseline(self, check: CheckConfig, now: float | None = None) -> float | None:
        """Median of this check's values from the baseline window, excluding the last two minutes."""
        now = now or time.time()
        oldest = now - check.baseline_minutes * 60
        values = [v for t, v in self.history[check.name] if v is not None and oldest <= t < now - RECENT_SECONDS]
        return statistics.median(values) if len(values) >= MIN_BASELINE_SAMPLES else None

    def run(self) -> list[Anomaly]:
        anomalies = []
        now = time.time()
        for c in self.checks:
            try:
                value = _first_number(self.sources[c.source].query(c.query))
            except SourceError as e:
                # A check that cannot run is itself worth investigating (DB down, auth broken...).
                self.last_values[c.name] = None
                anomalies.append(
                    Anomaly(c.name, c.service, None, c.threshold or 0.0, "error", f"check could not run: {e}")
                )
                continue
            self.last_values[c.name] = value
            baseline = self.baseline(c, now) if c.relative_factor else None
            self.history[c.name].append((now, value))

            if value is None:
                if c.no_data == "alert":
                    anomalies.append(Anomaly(c.name, c.service, None, c.threshold or 0.0, "no_data",
                                             f"no data: the check returned no value ({c.description})"))
            elif c.threshold is not None and _OPS[c.comparison](value, c.threshold):
                anomalies.append(Anomaly(c.name, c.service, value, c.threshold, c.comparison, c.description))
            elif baseline and value >= c.relative_min and value > c.relative_factor * baseline:
                anomalies.append(Anomaly(
                    c.name, c.service, value, round(c.relative_factor * baseline, 4), "x_normal",
                    f"{value / baseline:.1f}x its normal {baseline:g} over the last {c.baseline_minutes} min "
                    f"({c.description})",
                ))
        return anomalies
