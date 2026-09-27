"""Checkpoints for state snapshot and rollback."""

import time


class Checkpoint:
    """A saved state checkpoint for rollback."""

    def __init__(
        self,
        id: str,
        state: dict,
        description: str = "",
        created_at: float | None = None,
    ):
        self.id = id
        self.state = state
        self.description = description
        self.created_at = created_at or time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "state_keys": list(self.state.keys()),
            "created_at": self.created_at,
        }


class CheckpointManager:
    """Manages creation and restoration of checkpoints."""

    def __init__(self, max_checkpoints: int = 10):
        self.max_checkpoints = max_checkpoints
        self._checkpoints: dict[str, Checkpoint] = {}

    def create(self, task_id: str, state: dict, description: str = "") -> Checkpoint:
        cp = Checkpoint(
            id=f"ck_{task_id}_{int(time.time() * 1000)}",
            state=state,
            description=description,
        )
        self._checkpoints[cp.id] = cp
        self._prune()
        return cp

    def restore(self, checkpoint_id: str) -> dict | None:
        cp = self._checkpoints.get(checkpoint_id)
        if cp:
            return dict(cp.state)
        return None

    def list_checkpoints(self, task_id: str | None = None) -> list[dict]:
        cps = [
            cp for cp in self._checkpoints.values()
            if task_id is None or cp.id.startswith(f"ck_{task_id}_")
        ]
        return [cp.to_dict() for cp in cps]

    def _prune(self) -> None:
        if len(self._checkpoints) > self.max_checkpoints:
            oldest = sorted(self._checkpoints.values(), key=lambda c: c.created_at)
            for cp in oldest[: len(self._checkpoints) - self.max_checkpoints]:
                del self._checkpoints[cp.id]
