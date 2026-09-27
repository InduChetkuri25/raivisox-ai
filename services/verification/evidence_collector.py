"""Evidence collection for verification."""

import os

from services.verification.verification_engine import VerificationEvidence


class EvidenceCollector:
    """Collects diverse evidence types for verification."""

    def __init__(self):
        self._evidence: list[VerificationEvidence] = []

    def collect_status(self, task_id: str, status: str) -> VerificationEvidence:
        evidence = VerificationEvidence(
            id=f"status_{task_id}",
            type="status_check",
            description=f"Task status is '{status}'",
            passed=status in ("completed", "success", "done"),
            details={"status": status},
        )
        self._evidence.append(evidence)
        return evidence

    def collect_output(self, task_id: str, output: str, checks: list[str]) -> VerificationEvidence:
        missing = [c for c in checks if c.lower() not in output.lower()]
        evidence = VerificationEvidence(
            id=f"output_{task_id}",
            type="output_check",
            description=f"Output contains all required patterns: {checks}",
            passed=len(missing) == 0,
            details={
                "output_length": len(output),
                "checks": checks,
                "missing": missing,
            },
        )
        self._evidence.append(evidence)
        return evidence

    def collect_file_exists(self, task_id: str, path: str) -> VerificationEvidence:
        exists = os.path.exists(path)
        evidence = VerificationEvidence(
            id=f"file_{task_id}",
            type="file_existence",
            description=f"File exists: {path}",
            passed=exists,
            details={"path": path, "exists": exists},
        )
        self._evidence.append(evidence)
        return evidence

    def collect_metrics(self, task_id: str, metrics: dict) -> VerificationEvidence:
        evidence = VerificationEvidence(
            id=f"metrics_{task_id}",
            type="metrics_check",
            description="Task metrics within acceptable range",
            passed=bool(metrics.get("success", False)),
            details=metrics,
        )
        self._evidence.append(evidence)
        return evidence

    def get_all(self) -> list[VerificationEvidence]:
        return list(self._evidence)

    def clear(self) -> None:
        self._evidence.clear()
