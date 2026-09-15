"""Process-local operational metrics rendered in Prometheus text format.

Label values are always server-authored (route templates, allow-listed methods,
status classes, fixed reasons). User identifiers, paths, query strings and
content must never become labels.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable

LabelSet = tuple[tuple[str, str], ...]

_NAME = re.compile(r"^mia_[a-z0-9_]+$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_{}/:.\-]{0,128}$")
DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def _labels(labels: dict[str, str] | None) -> LabelSet:
    if not labels:
        return ()
    items = tuple(sorted(labels.items()))
    for _, value in items:
        if not _LABEL_VALUE.fullmatch(value):
            raise ValueError("metric label value is not server-authored")
    return items


def _render_labels(labels: LabelSet, extra: Iterable[tuple[str, str]] = ()) -> str:
    merged = (*labels, *extra)
    if not merged:
        return ""
    return "{" + ",".join(f'{key}="{value}"' for key, value in merged) + "}"


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


class MetricsRegistry:
    """Small thread-safe counter/gauge/histogram registry without external dependencies."""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = buckets
        self._counters: dict[str, dict[LabelSet, float]] = {}
        self._gauges: dict[str, dict[LabelSet, float]] = {}
        self._histograms: dict[str, dict[LabelSet, list[float]]] = {}

    @staticmethod
    def _check(name: str) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError("metric name must use the mia_ prefix")

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        self._check(name)
        key = _labels(labels)
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0.0) + value

    def add(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        self._check(name)
        key = _labels(labels)
        with self._lock:
            series = self._gauges.setdefault(name, {})
            series[key] = series.get(key, 0.0) + value

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        self._check(name)
        key = _labels(labels)
        with self._lock:
            self._gauges.setdefault(name, {})[key] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        self._check(name)
        key = _labels(labels)
        with self._lock:
            series = self._histograms.setdefault(name, {})
            # Layout: one cumulative count per bucket, then +Inf count, then sum.
            state = series.setdefault(key, [0.0] * (len(self._buckets) + 2))
            for index, bound in enumerate(self._buckets):
                if value <= bound:
                    state[index] += 1
            state[-2] += 1
            state[-1] += value

    def value(self, name: str, labels: dict[str, str] | None = None) -> float:
        """Return a counter or gauge value; intended for tests and diagnostics."""
        key = _labels(labels)
        with self._lock:
            if name in self._counters:
                return self._counters[name].get(key, 0.0)
            return self._gauges.get(name, {}).get(key, 0.0)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, series in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.extend(
                    f"{name}{_render_labels(key)} {_number(value)}"
                    for key, value in sorted(series.items())
                )
            for name, series in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.extend(
                    f"{name}{_render_labels(key)} {_number(value)}"
                    for key, value in sorted(series.items())
                )
            for name, histogram in sorted(self._histograms.items()):
                lines.append(f"# TYPE {name} histogram")
                for key, state in sorted(histogram.items()):
                    for index, bound in enumerate(self._buckets):
                        bucket_labels = _render_labels(key, (("le", f"{bound:g}"),))
                        lines.append(f"{name}_bucket{bucket_labels} {_number(state[index])}")
                    inf_labels = _render_labels(key, (("le", "+Inf"),))
                    lines.append(f"{name}_bucket{inf_labels} {_number(state[-2])}")
                    lines.append(f"{name}_count{_render_labels(key)} {_number(state[-2])}")
                    lines.append(f"{name}_sum{_render_labels(key)} {_number(state[-1])}")
        return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()
