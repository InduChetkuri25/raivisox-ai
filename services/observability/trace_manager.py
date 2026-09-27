"""Distributed trace management."""

import time
import uuid
from typing import Any


class Span:
    """A trace span."""

    def __init__(
        self,
        name: str,
        trace_id: str = "",
        parent_id: str = "",
        start_time: float | None = None,
    ):
        self.span_id = str(uuid.uuid4())
        self.trace_id = trace_id or self.span_id
        self.parent_id = parent_id
        self.name = name
        self.start_time = start_time or time.time()
        self.end_time: float | None = None
        self.attributes: dict[str, Any] = {}
        self._active = True

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        self.end_time = time.time()
        self._active = False

    @property
    def duration_ms(self) -> float:
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000

    @property
    def is_completed(self) -> bool:
        return self.end_time is not None

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "completed": self.is_completed,
        }


class TraceManager:
    """Manages distributed traces across the system."""

    def __init__(self):
        self._spans: dict[str, Span] = {}
        self._active_traces: set[str] = set()

    def start_span(self, name: str, trace_id: str = "", parent_id: str = "") -> Span:
        span = Span(name, trace_id, parent_id)
        self._spans[span.span_id] = span
        self._active_traces.add(span.trace_id)
        return span

    def get_span(self, span_id: str) -> Span | None:
        return self._spans.get(span_id)

    def get_trace(self, trace_id: str) -> list[Span]:
        return [s for s in self._spans.values() if s.trace_id == trace_id]

    def end_span(self, span_id: str) -> bool:
        span = self._spans.get(span_id)
        if span and span._active:
            span.end()
            trace_spans = [s for s in self._spans.values() if s.trace_id == span.trace_id]
            if all(s.is_completed for s in trace_spans):
                self._active_traces.discard(span.trace_id)
            return True
        return False

    def end_trace(self, trace_id: str) -> int:
        ended = 0
        for span in self._spans.values():
            if span.trace_id == trace_id and span._active:
                span.end()
                ended += 1
        self._active_traces.discard(trace_id)
        return ended

    def active_trace_count(self) -> int:
        return len(self._active_traces)

    def all_spans(self) -> list[Span]:
        return list(self._spans.values())

    def recent_traces(self, count: int = 10) -> list[list[Span]]:
        trace_ids = list({s.trace_id for s in self._spans.values()})
        return [self.get_trace(tid) for tid in trace_ids[-count:]]

    def summary(self) -> dict:
        total = len(self._spans)
        completed = sum(1 for s in self._spans.values() if s.is_completed)
        return {
            "total_spans": total,
            "completed_spans": completed,
            "active_traces": self.active_trace_count(),
            "avg_duration_ms": (
                sum(s.duration_ms for s in self._spans.values() if s.is_completed) / completed
                if completed > 0 else 0.0
            ),
        }

    def clear(self) -> None:
        self._spans.clear()
        self._active_traces.clear()
