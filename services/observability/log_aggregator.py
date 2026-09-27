"""Log aggregation across system components."""

import time
import uuid
from collections import defaultdict


class LogEntry:
    """A single log entry."""

    def __init__(
        self,
        level: str,
        message: str,
        source: str = "raivisox",
        data: dict | None = None,
        timestamp: float | None = None,
        request_id: str = "",
    ):
        self.level = level
        self.message = message
        self.source = source
        self.data = data or {}
        self.timestamp = timestamp or time.time()
        self.request_id = request_id or str(uuid.uuid4())

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "message": self.message,
            "source": self.source,
            "data": self.data,
            "timestamp": self.timestamp,
            "request_id": self.request_id,
        }


class LogAggregator:
    """Aggregates logs from all system components."""

    def __init__(self):
        self._logs: list[LogEntry] = []

    def log(
        self,
        level: str,
        message: str,
        source: str = "raivisox",
        data: dict | None = None,
        request_id: str = "",
        timestamp: float | None = None,
    ) -> LogEntry:
        entry = LogEntry(level, message, source, data, timestamp, request_id)
        self._logs.append(entry)
        return entry

    def info(self, message: str, source: str = "raivisox", data: dict | None = None, request_id: str = "") -> LogEntry:
        return self.log("INFO", message, source, data, request_id)

    def warning(self, message: str, source: str = "raivisox", data: dict | None = None, request_id: str = "") -> LogEntry:
        return self.log("WARNING", message, source, data, request_id)

    def error(self, message: str, source: str = "raivisox", data: dict | None = None, request_id: str = "") -> LogEntry:
        return self.log("ERROR", message, source, data, request_id)

    def debug(self, message: str, source: str = "raivisox", data: dict | None = None, request_id: str = "") -> LogEntry:
        return self.log("DEBUG", message, source, data, request_id)

    def get_logs(
        self,
        level: str | None = None,
        source: str | None = None,
        request_id: str | None = None,
        limit: int = 100,
    ) -> list[LogEntry]:
        logs = self._logs
        if level:
            logs = [l for l in logs if l.level == level]
        if source:
            logs = [l for l in logs if l.source == source]
        if request_id:
            logs = [l for l in logs if l.request_id == request_id]
        return logs[-limit:]

    def recent(self, count: int = 50) -> list[LogEntry]:
        return self._logs[-count:]

    def count_by_level(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for l in self._logs:
            counts[l.level] += 1
        return dict(counts)

    def clear(self) -> None:
        self._logs.clear()
