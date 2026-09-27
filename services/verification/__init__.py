"""Verification engine package."""

from services.verification.verification_engine import (
    VerificationEngine,
    VerificationResult,
    VerificationEvidence,
    Verdict,
)
from services.verification.evidence_collector import EvidenceCollector

__all__ = [
    "VerificationEngine",
    "VerificationResult",
    "VerificationEvidence",
    "Verdict",
    "EvidenceCollector",
]
