"""Metrics collection and aggregation."""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum


class MetricType(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass
class Metric:
    """A single metric data point."""

    name: str
    value: float
    metric_type: MetricType = MetricType.GAUGE
    labels: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "type": self.metric_type.value,
            "labels": self.labels,
            "timestamp": self.timestamp,
        }


class MetricsCollector:
    """Collects and aggregates metrics."""

    def __init__(self):
        self._metrics: list[Metric] = []
        self._counters: dict[str, float] = defaultdict(float)

    def record(
        self,
        name: str,
        value: float = 1.0,
        metric_type: MetricType = MetricType.GAUGE,
        labels: dict | None = None,
    ) -> Metric:
        metric = Metric(name=name, value=value, metric_type=metric_type, labels=labels or {})
        self._metrics.append(metric)
        if metric_type == MetricType.COUNTER:
            self._counters[name] += value
        return metric

    def increment(self, name: str, value: float = 1.0, labels: dict | None = None) -> Metric:
        return self.record(name, value, MetricType.COUNTER, labels)

    def gauge(self, name: str, value: float, labels: dict | None = None) -> Metric:
        return self.record(name, value, MetricType.GAUGE, labels)

    def histogram(self, name: str, value: float, labels: dict | None = None) -> Metric:
        return self.record(name, value, MetricType.HISTOGRAM, labels)

    def get(self, name: str) -> list[Metric]:
        return [m for m in self._metrics if m.name == name]

    def get_latest(self, name: str) -> Metric | None:
        ms = self.get(name)
        return ms[-1] if ms else None

    def counter_value(self, name: str) -> float:
        return self._counters.get(name, 0.0)

    def summary(self) -> dict:
        by_name = defaultdict(list)
        for m in self._metrics:
            by_name[m.name].append(m)
        return {
            "total_metrics": len(self._metrics),
            "unique_names": len(by_name),
            "counters": {n: self._counters[n] for n in self._counters},
        }

    def clear(self) -> None:
        self._metrics.clear()
        self._counters.clear()
