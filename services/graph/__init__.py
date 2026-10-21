"""V3 Phase 1 — provenance-aware event layer.

Phase 1 only *records* facts. Nothing in this package can authorize, deny or
otherwise change an execution outcome; the security kernel and the sandbox
remain the only authorities (see docs/v3-phase-1-spec.md §1.3).

Modules:
- ``schema``     closed sets, payload allowlists, target normalizers (no DB)
- ``actions``    per-process registry of live action ids (anti-spoof)
- ``provenance`` trusted identity resolution + ownership verification
- ``events``     the append-only writer and its bounded reads
"""
