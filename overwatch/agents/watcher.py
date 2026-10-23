"""Watcher: evaluates the configured checks. Deterministic on purpose: cheap, fast, no model call.

A check can alert on a fixed threshold, relative to its own recent normal ("5x the usual
latency"), when it returns no data, or any combination. A probe check calls a health URL itself.

A breach is only "suspected" until it has held for `confirm_samples` passes in a row; only
confirmed breaches open incidents, so a single blip shows on the map but pages nobody.
"""

from __future__ import annotations

import operator
import statistics
import time
from collections import deque

import httpx

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
    def __init__(
        self, checks: list[CheckConfig], sources: dict[str, DataSource], history_size: int = 720,
        confirm_samples: int = 1,
    ):
        self.checks = checks
        self.sources = sources
        self.confirm_samples = confirm_samples
        self.last_values: dict[str, float | None] = {}
        self.streak: dict[str, int] = {c.name: 0 for c in checks}  # consecutive breaching passes
        self.suspected: set[str] = set()  # breaching, not yet confirmed
        # check name -> (unix time, value) samples; feeds relative checks and the live charts
        self.history: dict[str, deque] = {c.name: deque(maxlen=history_size) for c in checks}

    def baseline(self, check: CheckConfig, now: float | None = None) -> float | None:
        """Median of this check's values from the baseline window, excluding the last two minutes."""
        now = now or time.time()
        oldest = now - check.baseline_minutes * 60
        values = [v for t, v in self.history[check.name] if v is not None and oldest <= t < now - RECENT_SECONDS]
        return statistics.median(values) if len(values) >= MIN_BASELINE_SAMPLES else None

    @staticmethod
    def _probe(c: CheckConfig) -> float:
        try:
            r = httpx.get(c.url, timeout=c.timeout_ms / 1000)
            return 1.0 if r.status_code < 400 else 0.0
        except httpx.HTTPError:
            return 0.0

    def run(self) -> list[Anomaly]:
        """One pass over every check; returns confirmed anomalies (see `suspected` for the rest)."""
        raw = self._evaluate()
        confirmed, self.suspected = [], set()
        breaching = {a.check for a in raw}
        for c in self.checks:
            self.streak[c.name] = self.streak.get(c.name, 0) + 1 if c.name in breaching else 0
        for a in raw:
            if self.streak[a.check] >= self.confirm_samples:
                confirmed.append(a)
            else:
                self.suspected.add(a.check)
        return confirmed

    def _evaluate(self) -> list[Anomaly]:
        anomalies = []
        now = time.time()
        for c in self.checks:
            try:
                if c.kind == "probe":
                    value: float | None = self._probe(c)
                else:
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
