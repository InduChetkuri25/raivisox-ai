"""Artifact retention cleanup — safe, deterministic byte reaping.

Policy (docs/deployment.md):
- `ARTIFACT_RETENTION_DAYS=0` (default) → keep artifact bytes forever;
  `cleanup_expired_artifacts` is a no-op and the scheduler never runs.
- `N > 0` → two deterministic passes:
  * Pass A — registry-driven: the physical bytes of rows that are no
    longer `is_current`, older than N days, and whose path is NOT
    referenced by any current row.
  * Pass B — orphan GC: files inside the workspace older than N days that
    NO registry row (current or superseded) references — never-registered
    step leftovers and files of deleted tasks (rows cascade away with the
    task). Pure garbage collection; there is no metadata to lose.

Registry rows are NEVER deleted by either pass: history, stable artifact
IDs, run grouping and every metadata field remain queryable; downloads and
ZIPs already report missing bytes honestly (`exists: false`, `MISSING.txt`,
historical 404).

Safety rules enforced here:
- Current rows are never touched (Pass A filters them out; Pass B protects
  every referenced path) — a live deliverable can never be reaped because
  an older record referenced the same path (same-path reruns reuse paths).
- mtime guard (Pass A defense in depth): a superseded row whose file was
  rewritten after its supersession timestamp is skipped even when no
  current row references the path — the bytes are not the ones the row
  recorded.
- containment: Pass A resolves and checks every row path against the
  workspace root; Pass B never follows symlinks and re-checks containment
  of every walked file. Anything outside the root is counted, never
  deleted, never logged as a path.
- directories are never deleted (files only; directory structure stays).
- tolerates already-missing files (counted as `already_missing`).
- statistics only — no secrets, no filesystem paths in logs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

logger = logging.getLogger("raivisox.artifact_cleanup")


@dataclass
class CleanupStats:
    """Safe operational statistics for one cleanup pass.

    Counts only — never paths, never hashes, never filenames (a filename
    could be sensitive in a multi-tenant deployment).
    """

    scanned: int = 0
    deleted: int = 0
    bytes_freed: int = 0
    skipped_current_path: int = 0
    skipped_not_expired: int = 0
    skipped_path_unsafe: int = 0
    skipped_file_modified: int = 0
    already_missing: int = 0
    errors: int = 0
    # Pass B — orphan files (no registry row references them at all).
    orphans_seen: int = 0
    orphans_deleted: int = 0
    orphan_bytes_freed: int = 0
    orphans_protected_registered: int = 0
    retention_disabled: bool = False

    def safe_summary(self) -> dict:
        return {
            "scanned": self.scanned,
            "deleted": self.deleted,
            "bytes_freed": self.bytes_freed,
            "skipped_current_path": self.skipped_current_path,
            "skipped_not_expired": self.skipped_not_expired,
            "skipped_path_unsafe": self.skipped_path_unsafe,
            "skipped_file_modified": self.skipped_file_modified,
            "already_missing": self.already_missing,
            "errors": self.errors,
            "orphans_seen": self.orphans_seen,
            "orphans_deleted": self.orphans_deleted,
            "orphan_bytes_freed": self.orphan_bytes_freed,
            "orphans_protected_registered": self.orphans_protected_registered,
            "retention_disabled": self.retention_disabled,
        }


def _contained(root: str, resolved: str) -> bool:
    """True when `resolved` is strictly inside `root` (same os, realpath)."""
    return resolved.startswith(root + os.sep)


def _safe_mtime(path: str) -> float | None:
    """File mtime, or None when unreadable/missing."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _delete_bytes(resolved: str, size: int, stats: CleanupStats) -> None:
    """os.remove with error containment and stat accounting."""
    try:
        os.remove(resolved)
        stats.deleted += 1
        stats.bytes_freed += size
    except OSError:
        stats.errors += 1


async def _all_registered_paths(db: AsyncSession) -> set[str]:
    """Realpaths of every storage_path in the registry (current + superseded).

    Used by the orphan pass: a path any row commits to is never garbage —
    superseded rows' paths hold their successor's current bytes, and a
    historical row's identity still refers to that location.
    """
    from app.models.task_artifact import TaskArtifact

    rows = (
        (await db.execute(select(TaskArtifact.storage_path))).scalars().all()
    )
    return {os.path.realpath(p) for p in rows}


async def _current_paths(db: AsyncSession) -> set[str]:
    """Realpaths of every storage_path referenced by a CURRENT row."""
    from app.models.task_artifact import TaskArtifact

    rows = (
        (
            await db.execute(
                select(TaskArtifact.storage_path).where(
                    TaskArtifact.is_current == True  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    return {os.path.realpath(p) for p in rows}


async def find_expired_artifacts(
    db: AsyncSession,
    *,
    root: str,
    retention_days: int,
    now: datetime | None = None,
) -> List[Tuple[object, str]]:
    """Return (row, resolved_path) pairs eligible for byte deletion.

    Eligible = `is_current` is False AND `created_at` older than
    `retention_days`. Containment is checked here so every caller (API,
    scheduler, tests) gets the same filtering. `now` is injectable for
    deterministic tests. Callers MUST still apply the current-path and
    mtime guards before deleting (see cleanup_expired_artifacts).
    """
    from app.models.task_artifact import TaskArtifact

    if retention_days <= 0:
        return []
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=retention_days)
    real_root = os.path.realpath(root)

    rows = (
        (
            await db.execute(
                select(TaskArtifact)
                .where(
                    TaskArtifact.is_current == False,  # noqa: E712
                    TaskArtifact.created_at < cutoff,
                )
                .order_by(TaskArtifact.created_at.asc(), TaskArtifact.id.asc())
            )
        )
        .scalars()
        .all()
    )
    out: List[Tuple[object, str]] = []
    for r in rows:
        resolved = os.path.realpath(r.storage_path)
        if not _contained(real_root, resolved):
            continue
        out.append((r, resolved))
    return out


async def _cleanup_orphans(
    db: AsyncSession,
    *,
    root: str,
    cutoff: datetime,
    stats: CleanupStats,
) -> None:
    """Pass B — delete aged files that NO registry row references.

    Walks the workspace root (files only, directories never removed),
    protects every registered path, every symlink, and everything outside
    the root. A file's own mtime decides its age: bytes written within the
    retention window are never garbage, so an in-flight execution's fresh
    writes are untouchable.
    """
    real_root = os.path.realpath(root)
    if not os.path.isdir(real_root):
        return
    registered = await _all_registered_paths(db)
    cutoff_ts = cutoff.timestamp()
    for dirpath, _dirnames, filenames in os.walk(real_root, followlinks=False):
        for name in filenames:
            raw = os.path.join(dirpath, name)
            stats.orphans_seen += 1
            if os.path.islink(raw):
                stats.skipped_path_unsafe += 1
                continue
            resolved = os.path.realpath(raw)
            if not _contained(real_root, resolved) or resolved in registered:
                # Outside the root, or the registry commits to these bytes
                # (current deliverable or superseded history location).
                stats.orphans_protected_registered += 1
                continue
            mtime = _safe_mtime(resolved)
            if mtime is None:
                continue
            if mtime >= cutoff_ts:
                stats.skipped_not_expired += 1
                continue
            size = _safe_size(resolved)
            try:
                os.remove(resolved)
                stats.orphans_deleted += 1
                stats.orphan_bytes_freed += size
            except OSError:
                stats.errors += 1


async def cleanup_expired_artifacts(
    db: AsyncSession,
    *,
    root: str,
    settings: Settings,
    now: datetime | None = None,
) -> CleanupStats:
    """Run one retention pass (A + B) and return safe statistics.

    Deterministic order: Pass A by (created_at, id); Pass B in filesystem
    walk order. Per-file errors are counted and skipped so one unreadable
    file cannot block the pass; only an unusable DB session propagates.
    """
    stats = CleanupStats()
    retention = int(getattr(settings, "artifact_retention_days", 0) or 0)
    if retention <= 0:
        # Retention disabled: nothing is ever eligible. Metadata untouched.
        stats.retention_disabled = True
        return stats

    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=retention)
    real_root = os.path.realpath(root)
    current_paths = await _current_paths(db)

    # ---- Pass A: registry-driven ------------------------------------------
    expired = await find_expired_artifacts(
        db, root=root, retention_days=retention, now=now
    )
    for r, resolved in expired:
        stats.scanned += 1
        # Guard 1 — path owned by a live registration (path reuse). The
        # physical bytes on a shared path belong to the CURRENT row.
        if resolved in current_paths:
            stats.skipped_current_path += 1
            continue
        # Guard 2 — containment (defense in depth; find_ filtered already).
        if not _contained(real_root, resolved):
            stats.skipped_path_unsafe += 1
            continue
        if not os.path.isfile(resolved):
            stats.already_missing += 1
            continue
        # Guard 3 — mtime: the row's bytes must still be the file's bytes.
        mtime = _safe_mtime(resolved)
        if mtime is not None:
            ref = r.superseded_at or r.created_at
            if ref is not None and mtime > ref.timestamp():
                stats.skipped_file_modified += 1
                continue
        _delete_bytes(resolved, os.path.getsize(resolved), stats)

    # ---- Pass B: orphan GC --------------------------------------------------
    await _cleanup_orphans(db, root=real_root, cutoff=cutoff, stats=stats)
    return stats


async def run_cleanup_with_own_session(root: str, settings: Settings) -> dict:
    """Run one cleanup pass on a fresh session (scheduler/endpoint entry).

    Concurrency against registration: `_register_task_artifact` supersedes
    the old current row and commits before this pass reads rows, so a rerun
    on the same path leaves the old row non-current and the path owned by
    the new CURRENT row — guard 1 then protects the new bytes. A row that
    is current at cleanup time is never deleted regardless of age, so a
    newly-created artifact can never be reaped because an older record
    happened to reference the same path. The orphan pass only removes
    bytes older than the retention window that no row references — a
    just-written file has a fresh mtime and cannot be reaped mid-execution.
    """
    from app.database import async_session_factory

    async with async_session_factory() as db:
        stats = await cleanup_expired_artifacts(db, root=root, settings=settings)
        await db.commit()
        return stats.safe_summary()
