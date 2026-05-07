"""Lightweight in-memory metrics for KERN.

No external dependencies â€” counters and histograms stored in a thread-safe
dict, exposed as JSON via the ``/metrics`` endpoint.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Generator


class _Metrics:
    """Process-global metrics store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, list[float]] = {}

    # -- Counters --

    def inc(self, name: str, amount: int = 1, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def counter_value(self, name: str, labels: dict[str, str] | None = None) -> int:
        return self._counters.get(self._key(name, labels), 0)

    # -- Histograms --

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            bucket = self._histograms.setdefault(name, [])
            bucket.append(value)
            # Keep last 1000 observations to bound memory
            if len(bucket) > 1000:
                bucket[:] = bucket[-1000:]

    @contextmanager
    def timer(self, name: str) -> Generator[None, None, None]:
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, time.monotonic() - t0)

    # -- Snapshot --

    def snapshot(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
            histograms = {}
            for name, values in self._histograms.items():
                if values:
                    sorted_v = sorted(values)
                    histograms[name] = {
                        "count": len(sorted_v),
                        "sum": round(sum(sorted_v), 4),
                        "min": round(sorted_v[0], 4),
                        "max": round(sorted_v[-1], 4),
                        "p50": round(sorted_v[len(sorted_v) // 2], 4),
                        "p99": round(sorted_v[int(len(sorted_v) * 0.99)], 4),
                    }
        return {"counters": counters, "histograms": histograms}

    # -- Helpers --

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> str:
        if not labels:
            return name
        suffix = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{suffix}}}"


# Module-level singleton
metrics = _Metrics()
