"""Recovery engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time


class RecoveryOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ESCALATED = "escalated"
    NO_ACTION_NEEDED = "no_action_needed"


@dataclass
class RecoveryAction:
    """A single recovery action taken."""

    id: str
    type: str
    description: str
    result: RecoveryOutcome
    details: dict = field(default_factory=dict)
    attempted_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "result": self.result.value,
            "details": self.details,
            "attempted_at": self.attempted_at,
        }


@dataclass
class RecoveryResult:
    """Result of a recovery attempt."""

    task_id: str
    outcome: RecoveryOutcome
    actions: list[RecoveryAction] = field(default_factory=list)
    recovered: bool = False
    final_status: str | None = None
    recovery_time: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "actions_taken": [a.to_dict() for a in self.actions],
            "recovered": self.recovered,
            "final_status": self.final_status,
            "recovery_time": self.recovery_time,
            "notes": self.notes,
        }


class RecoveryStrategy(Enum):
    RETRY = "retry"
    ROLLBACK = "rollback"
    REPLACE_MODEL = "replace_model"
    FALLBACK_TOOL = "fallback_tool"
    SKIP_AND_CONTINUE = "skip_and_continue"
    ESCALATE = "escalate"
    REPLAN = "replan"


class RecoveryEngine:
    """Orchestrates recovery strategies when task execution fails."""

    def __init__(self):
        self._strategies: dict[RecoveryStrategy, bool] = {
            RecoveryStrategy.RETRY: True,
            RecoveryStrategy.ROLLBACK: True,
            RecoveryStrategy.REPLACE_MODEL: True,
            RecoveryStrategy.FALLBACK_TOOL: True,
            RecoveryStrategy.SKIP_AND_CONTINUE: True,
            RecoveryStrategy.ESCALATE: False,
            RecoveryStrategy.REPLAN: True,
        }
        self._attempt_history: list[RecoveryResult] = []

    def enable_strategy(self, strategy: RecoveryStrategy) -> None:
        self._strategies[strategy] = True

    def disable_strategy(self, strategy: RecoveryStrategy) -> None:
        self._strategies[strategy] = False

    def recover(
        self,
        task_id: str,
        failure_info: dict,
        max_retries: int = 3,
    ) -> RecoveryResult:
        """Attempt to recover from a task failure using available strategies."""
        start = time.time()
        actions: list[RecoveryAction] = []
        recovered = False
        final_status = None

        # Strategy 1: Retry
        if self._strategies[RecoveryStrategy.RETRY]:
            retry_attempts = failure_info.get("retry_count", 0)
            if retry_attempts < max_retries:
                action = RecoveryAction(
                    id=f"retry_{task_id}_{retry_attempts + 1}",
                    type="retry",
                    description=f"Retry task (attempt {retry_attempts + 1}/{max_retries})",
                    result=RecoveryOutcome.SUCCESS,
                    details={"attempt": retry_attempts + 1, "max_retries": max_retries},
                )
                actions.append(action)
                recovered = True
                final_status = "pending_retry"

        # Strategy 2: Rollback
        if not recovered and self._strategies[RecoveryStrategy.ROLLBACK]:
            rollback_point = failure_info.get("rollback_point")
            if rollback_point:
                action = RecoveryAction(
                    id=f"rollback_{task_id}",
                    type="rollback",
                    description=f"Rollback to checkpoint: {rollback_point}",
                    result=RecoveryOutcome.SUCCESS,
                    details={"checkpoint": rollback_point},
                )
                actions.append(action)
                recovered = True
                final_status = "rolled_back"

        # Strategy 3: Replan
        if not recovered and self._strategies[RecoveryStrategy.REPLAN]:
            action = RecoveryAction(
                id=f"replan_{task_id}",
                type="replan",
                description="Generate alternative execution plan",
                result=RecoveryOutcome.SUCCESS,
                details={"reason": failure_info.get("error", "unknown failure")},
            )
            actions.append(action)
            recovered = True
            final_status = "replanned"

        # Strategy 4: Fallback tool
        if not recovered and self._strategies[RecoveryStrategy.FALLBACK_TOOL]:
            primary_tool = failure_info.get("failed_tool")
            fallback = failure_info.get("fallback_tool")
            if fallback:
                action = RecoveryAction(
                    id=f"fallback_{task_id}",
                    type="fallback_tool",
                    description=f"Switch from '{primary_tool}' to '{fallback}'",
                    result=RecoveryOutcome.SUCCESS,
                    details={"from": primary_tool, "to": fallback},
                )
                actions.append(action)
                recovered = True
                final_status = "fallback_applied"

        # Strategy 5: Escalation
        if not recovered and self._strategies[RecoveryStrategy.ESCALATE]:
            action = RecoveryAction(
                id=f"escalate_{task_id}",
                type="escalate",
                description="Escalate to human operator",
                result=RecoveryOutcome.ESCALATED,
                details={"reason": failure_info.get("error", "unknown")},
            )
            actions.append(action)

        recovery_time = time.time() - start
        outcome = RecoveryOutcome.SUCCESS if recovered else (
            RecoveryOutcome.ESCALATED if any(a.result == RecoveryOutcome.ESCALATED for a in actions)
            else RecoveryOutcome.FAILED
        )

        result = RecoveryResult(
            task_id=task_id,
            outcome=outcome,
            actions=actions,
            recovered=recovered,
            final_status=final_status,
            recovery_time=recovery_time,
            notes=f"Recovery attempted using {len(actions)} strategy/strategies",
        )
        self._attempt_history.append(result)
        return result

    def get_history(self, task_id: str | None = None) -> list[RecoveryResult]:
        if task_id:
            return [r for r in self._attempt_history if r.task_id == task_id]
        return list(self._attempt_history)

    def success_rate(self) -> float:
        if not self._attempt_history:
            return 0.0
        return sum(1 for r in self._attempt_history if r.recovered) / len(self._attempt_history)
