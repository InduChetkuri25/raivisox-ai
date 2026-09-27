"""Experience memory and strategy learning."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time
import uuid


class StrategyLearningSource(str, Enum):
    EXECUTION_OUTCOME = "execution_outcome"
    VERIFICATION_RESULT = "verification_result"
    RECOVERY_ATTEMPT = "recovery_attempt"
    USER_FEEDBACK = "user_feedback"


@dataclass
class ExperienceEntry:
    """A single experience entry for learning."""

    id: str
    task_type: str
    strategy_used: str
    outcome: str  # success, partial, failed
    metrics: dict = field(default_factory=dict)
    observations: list[str] = field(default_factory=list)
    source: StrategyLearningSource = StrategyLearningSource.EXECUTION_OUTCOME
    created_at: float = field(default_factory=time.time)
    weight: float = 1.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "strategy_used": self.strategy_used,
            "outcome": self.outcome,
            "metrics": self.metrics,
            "observations": self.observations,
            "source": self.source.value,
            "created_at": self.created_at,
            "weight": self.weight,
        }


class LearnedStrategy:
    """A strategy that has been learned from experience."""

    def __init__(
        self,
        strategy_name: str,
        task_type: str,
        success_rate: float = 0.0,
        avg_latency_ms: float = 0.0,
        avg_cost: float = 0.0,
        confidence: float = 0.0,
        total_attempts: int = 0,
        last_used: float | None = None,
        notes: str = "",
    ):
        self.strategy_name = strategy_name
        self.task_type = task_type
        self.success_rate = success_rate
        self.avg_latency_ms = avg_latency_ms
        self.avg_cost = avg_cost
        self.confidence = confidence
        self.total_attempts = total_attempts
        self.last_used = last_used or time.time()
        self.notes = notes

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "task_type": self.task_type,
            "success_rate": self.success_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_cost": self.avg_cost,
            "confidence": self.confidence,
            "total_attempts": self.total_attempts,
            "last_used": self.last_used,
            "notes": self.notes,
        }


class ExperienceMemory:
    """Stores and retrieves execution experiences for learning."""

    def __init__(self):
        self._entries: list[ExperienceEntry] = []
        self._task_type_index: dict[str, list[int]] = defaultdict(list)

    def add(
        self,
        task_type: str,
        strategy: str,
        outcome: str,
        metrics: dict | None = None,
        observations: list[str] | None = None,
        source: StrategyLearningSource = StrategyLearningSource.EXECUTION_OUTCOME,
        weight: float = 1.0,
    ) -> ExperienceEntry:
        entry = ExperienceEntry(
            id=str(uuid.uuid4()),
            task_type=task_type,
            strategy_used=strategy,
            outcome=outcome,
            metrics=metrics or {},
            observations=observations or [],
            source=source,
            weight=weight,
        )
        self._entries.append(entry)
        self._task_type_index[task_type].append(len(self._entries) - 1)
        return entry

    def get_by_task_type(self, task_type: str, limit: int = 50) -> list[ExperienceEntry]:
        indices = self._task_type_index.get(task_type, [])
        entries = [self._entries[i] for i in indices if i < len(self._entries)]
        return entries[-limit:]

    def get_recent(self, limit: int = 100) -> list[ExperienceEntry]:
        return self._entries[-limit:]

    def get_by_outcome(self, outcome: str) -> list[ExperienceEntry]:
        return [e for e in self._entries if e.outcome == outcome]

    def get_by_strategy(self, strategy: str) -> list[ExperienceEntry]:
        return [e for e in self._entries if e.strategy_used == strategy]

    def success_rate_for(self, task_type: str, strategy: str) -> float:
        entries = self.get_by_task_type(task_type)
        relevant = [e for e in entries if e.strategy_used == strategy]
        if not relevant:
            return 0.0
        return sum(1 for e in relevant if e.outcome == "success") / len(relevant)

    def average_metrics_for(self, task_type: str, strategy: str, metric_name: str) -> float:
        entries = self.get_by_task_type(task_type)
        relevant = [e for e in entries if e.strategy_used == strategy and metric_name in e.metrics]
        if not relevant:
            return 0.0
        return sum(e.metrics[metric_name] * e.weight for e in relevant) / sum(e.weight for e in relevant)

    def total_entries(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._task_type_index.clear()


class StrategyLearner:
    """Learns optimal strategies from accumulated experience."""

    def __init__(self, memory: ExperienceMemory):
        self.memory = memory

    def learn_for(self, task_type: str) -> list[LearnedStrategy]:
        """Return learned strategies for a task type, ranked by confidence."""
        entries = self.memory.get_by_task_type(task_type)
        strategies: dict[str, dict] = defaultdict(lambda: {
            "attempts": 0,
            "successes": 0,
            "total_latency": 0.0,
            "total_cost": 0.0,
        })

        for e in entries:
            s = strategies[e.strategy_used]
            s["attempts"] += 1
            if e.outcome == "success":
                s["successes"] += 1
            s["total_latency"] += e.metrics.get("latency_ms", 0) * e.weight
            s["total_cost"] += e.metrics.get("cost", 0) * e.weight

        learned = []
        for name, data in strategies.items():
            success_rate = data["successes"] / data["attempts"] if data["attempts"] > 0 else 0.0
            avg_latency = data["total_latency"] / data["attempts"] if data["attempts"] > 0 else 0.0
            avg_cost = data["total_cost"] / data["attempts"] if data["attempts"] > 0 else 0.0
            confidence = success_rate * 0.6 + (1.0 / (1.0 + avg_latency / 1000)) * 0.2 + (1.0 / (1.0 + avg_cost)) * 0.2

            learned.append(LearnedStrategy(
                strategy_name=name,
                task_type=task_type,
                success_rate=success_rate,
                avg_latency_ms=avg_latency,
                avg_cost=avg_cost,
                confidence=confidence,
                total_attempts=data["attempts"],
            ))

        return sorted(learned, key=lambda s: s.confidence, reverse=True)

    def recommended_strategy(self, task_type: str) -> str | None:
        """Get the best strategy for a task type based on learned experience."""
        learned = self.learn_for(task_type)
        return learned[0].strategy_name if learned else None

    def should_update_strategy(self, task_type: str, strategy: str, min_samples: int = 5) -> bool:
        """Check if enough data exists to recommend a strategy."""
        entries = self.memory.get_by_task_type(task_type)
        relevant = [e for e in entries if e.strategy_used == strategy]
        return len(relevant) >= min_samples

    def get_all_learned_strategies(self) -> list[LearnedStrategy]:
        """Get all learned strategies across all task types."""
        all_strategies: dict[str, LearnedStrategy] = {}
        for task_type in self.memory._task_type_index:
            for ls in self.learn_for(task_type):
                key = f"{task_type}:{ls.strategy_name}"
                if key not in all_strategies or ls.confidence > all_strategies[key].confidence:
                    all_strategies[key] = ls
        return sorted(all_strategies.values(), key=lambda s: s.confidence, reverse=True)


# For type annotation
from collections import defaultdict
