"""Execution observation — records what happened during task execution."""

import time


class Observation:
    """A single observation point during execution."""

    def __init__(
        self,
        node_id: str,
        event: str,
        data: dict | None = None,
        timestamp: float | None = None,
    ):
        self.node_id = node_id
        self.event = event
        self.data = data or {}
        self.timestamp = timestamp or time.time()

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "event": self.event,
            "data": self.data,
            "timestamp": self.timestamp,
        }


class ObservationEngine:
    """Collects and manages observations during task execution."""

    def __init__(self):
        self.observations: list[Observation] = []

    def record(self, node_id: str, event: str, data: dict | None = None) -> Observation:
        obs = Observation(node_id, event, data)
        self.observations.append(obs)
        return obs

    def get_for_node(self, node_id: str) -> list[Observation]:
        return [o for o in self.observations if o.node_id == node_id]

    def get_by_event(self, event: str) -> list[Observation]:
        return [o for o in self.observations if o.event == event]

    def summary(self) -> dict:
        events: dict[str, int] = {}
        for o in self.observations:
            events[o.event] = events.get(o.event, 0) + 1
        return {
            "total": len(self.observations),
            "events": events,
            "nodes_observed": len({o.node_id for o in self.observations}),
        }
