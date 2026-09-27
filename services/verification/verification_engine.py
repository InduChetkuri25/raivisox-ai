"""Verification engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time


class Verdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass
class VerificationEvidence:
    """A single piece of verification evidence."""

    id: str
    type: str
    description: str
    passed: bool
    details: dict = field(default_factory=dict)
    collected_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "passed": self.passed,
            "details": self.details,
            "collected_at": self.collected_at,
        }


@dataclass
class VerificationResult:
    """Result of a verification run."""

    task_id: str
    verdict: Verdict
    confidence: float = 0.0
    evidence: list[VerificationEvidence] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    verification_type: str = "basic"
    details: dict = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return self.verdict in (Verdict.PASSED, Verdict.PARTIAL)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "evidence_count": len(self.evidence),
            "failed_checks": self.failed_checks,
            "verification_type": self.verification_type,
            "details": self.details,
            "passed": self.passed,
            "checked_at": self.checked_at,
        }


class VerificationEngine:
    """Independent verification engine that checks task outcomes against criteria."""

    def __init__(self):
        self._checks: list[dict] = []

    def register_check(self, name: str, check_fn, required: bool = True) -> None:
        self._checks.append({"name": name, "fn": check_fn, "required": required})

    def verify(
        self,
        task_id: str,
        outcome: Any,
        criteria: dict | None = None,
    ) -> VerificationResult:
        """Run all registered checks against the outcome."""
        evidence: list[VerificationEvidence] = []
        failed_checks: list[str] = []

        for check in self._checks:
            try:
                result = check["fn"](outcome, criteria or {})
                passed = bool(result.get("passed", False)) if isinstance(result, dict) else bool(result)
                evidence.append(
                    VerificationEvidence(
                        id=check["name"],
                        type=check["name"],
                        description=check["name"],
                        passed=passed,
                        details=result if isinstance(result, dict) else {},
                    )
                )
                if not passed and check["required"]:
                    failed_checks.append(check["name"])
            except Exception as e:
                evidence.append(
                    VerificationEvidence(
                        id=check["name"],
                        type=check["name"],
                        description=f"Check error: {e}",
                        passed=False,
                        details={"error": str(e)},
                    )
                )
                if check["required"]:
                    failed_checks.append(check["name"])

        passed_count = sum(1 for e in evidence if e.passed)
        total = len(evidence) or 1
        confidence = passed_count / total

        if not failed_checks:
            verdict = Verdict.PASSED
        elif passed_count > 0:
            verdict = Verdict.PARTIAL
        else:
            verdict = Verdict.FAILED

        return VerificationResult(
            task_id=task_id,
            verdict=verdict,
            confidence=confidence,
            evidence=evidence,
            failed_checks=failed_checks,
            details={"passed_checks": passed_count, "total_checks": total},
        )

    def verify_task_completed(self, task_id: str, outcome: Any) -> VerificationResult:
        """Quick check: was the task completed successfully?"""
        return self.verify(task_id, outcome)

    def reset(self) -> None:
        self._checks.clear()
