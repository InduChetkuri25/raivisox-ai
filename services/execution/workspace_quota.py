"""Workspace write quotas — the application-layer T8 control (V2 closeout).

WHAT THIS IS
------------
A quota on what a task may write **inside its own workspace scope**:
total bytes, per-file bytes, entry count (files + directories, hidden
entries included), path depth, and a fail-closed rule against links that
resolve outside the scope. It is enforced *before* a write lands wherever
the application performs the write (``fs_write``), and *around* every
execution that can generate files (the sandbox pre-run gate refuses a run
that could only grow an already-exhausted workspace; the post-run
measurement records what the child actually produced and reports a
violation honestly).

WHAT THIS IS NOT
----------------
It is **not** a filesystem-wide disk quota. Two consequences, stated
plainly rather than papered over:

1. A child process inside the sandbox can still write until the
   *per-file* limit (``RLIMIT_FSIZE`` / ``--ulimit fsize``) stops it, and
   can keep creating sub-limit files until the run's wall-clock timeout
   or the post-run measurement reports the violation. Stopping that
   mid-write needs an OS mechanism (per-container storage quota,
   project/XFS quota, a size-bounded volume), which this module does not
   claim to provide.
2. A hardlink created inside the scope that points at a file outside it
   (same filesystem) bypasses "bytes live inside the workspace" reasoning
   entirely. Hardlink creation cannot be intercepted at this layer; the
   measurement counts a hardlinked inode once, so it never *double*
   counts, and the limitation is documented instead of hidden.

Measurement rules (all of them chosen so an attacker cannot hide bytes):

- ``os.lstat`` only — symlinks are counted, never followed, so a link to
  a huge file outside cannot inflate usage and a link to a device/FIFO is
  never opened.
- A symlink whose resolution leaves the root is an ``escaped_symlink`` and
  is treated as a violation (fail closed) rather than ignored: ignoring it
  would let a child place a link that the *next* app-layer write follows.
- Hardlinked inodes are counted once per inode, never once per name.
- A directory that cannot be listed is counted (``unreadable``) and the
  quota refuses further writes: an unreadable subtree could hide bytes, so
  under-counting is not allowed to become a bypass.
- Special files (FIFO/socket/device) are counted but never opened.

Byte accounting on write uses the size the caller is about to write, plus
the measured total, minus the bytes the write replaces (a rewrite is not
double-counted), plus any in-flight reservations from concurrent writes in
this process. Pending bytes are always released, including on exceptions.
"""

from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass
from typing import Iterator

from services.security.path_containment import contained_path

__all__ = [
    "WorkspaceQuota",
    "WorkspaceUsage",
    "WorkspaceQuotaExceeded",
    "WorkspaceEnforcer",
    "WorkspaceQuotaCache",
    "measure_workspace",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_DEPTH",
]

_MIB = 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * _MIB
DEFAULT_MAX_FILE_BYTES = 8 * _MIB
DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_MAX_DEPTH = 32


def _env_limit(name: str, default: int | None) -> int | None:
    """Read a byte/entry limit from the environment.

    Absent → ``default``. ``0``/``off``/``none``/``unlimited`` → ``None``
    (explicitly disabled — a deliberate weakening, never the default).
    Anything else must be a non-negative integer; a malformed value raises
    instead of silently falling back to a weaker setting.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in ("", "0", "off", "none", "unlimited"):
        return None
    try:
        value = int(token)
    except ValueError:
        raise ValueError(
            f"{name} must be a non-negative integer (or 0/off to disable); "
            f"got {raw!r}"
        ) from None
    if value < 0:
        raise ValueError(f"{name} must be non-negative; got {raw!r}")
    return value


@dataclass(frozen=True)
class WorkspaceQuota:
    """The workspace write grant for one scope.

    ``None`` disables a dimension. Defaults are generous enough not to
    interfere with normal pipeline output while still bounding a runaway
    step; a caller that wants strict limits passes them explicitly.
    """

    max_total_bytes: int | None = DEFAULT_MAX_TOTAL_BYTES
    max_file_bytes: int | None = DEFAULT_MAX_FILE_BYTES
    max_entries: int | None = DEFAULT_MAX_ENTRIES
    max_depth: int | None = DEFAULT_MAX_DEPTH
    allow_escaped_symlinks: bool = False

    @classmethod
    def from_env(cls) -> "WorkspaceQuota":
        """Build from ``SANDBOX_WORKSPACE_*`` environment variables."""
        return cls(
            max_total_bytes=_env_limit("SANDBOX_WORKSPACE_MAX_TOTAL_BYTES",
                                       DEFAULT_MAX_TOTAL_BYTES),
            max_file_bytes=_env_limit("SANDBOX_WORKSPACE_MAX_FILE_BYTES",
                                      DEFAULT_MAX_FILE_BYTES),
            max_entries=_env_limit("SANDBOX_WORKSPACE_MAX_ENTRIES",
                                   DEFAULT_MAX_ENTRIES),
            max_depth=_env_limit("SANDBOX_WORKSPACE_MAX_DEPTH",
                                 DEFAULT_MAX_DEPTH),
        )

    def describe(self) -> dict:
        """Honest, serializable view of what is actually enforced."""
        return {
            "max_total_bytes": self.max_total_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_entries": self.max_entries,
            "max_depth": self.max_depth,
            "allow_escaped_symlinks": self.allow_escaped_symlinks,
            "scope": "workspace-write-only (not a filesystem-wide quota)",
        }


@dataclass
class WorkspaceUsage:
    """What a workspace currently contains (measured, never followed)."""

    total_bytes: int = 0
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    escaped_symlinks: int = 0
    special_files: int = 0
    unreadable: int = 0
    max_depth: int = 0
    largest_file_bytes: int = 0

    @property
    def entries(self) -> int:
        return self.files + self.directories

    def status(self) -> dict:
        return {
            "total_bytes": self.total_bytes,
            "files": self.files,
            "directories": self.directories,
            "entries": self.entries,
            "symlinks": self.symlinks,
            "escaped_symlinks": self.escaped_symlinks,
            "special_files": self.special_files,
            "unreadable": self.unreadable,
            "max_depth": self.max_depth,
            "largest_file_bytes": self.largest_file_bytes,
        }


def measure_workspace(root: str) -> WorkspaceUsage:
    """Measure ``root`` without following symlinks or opening any file.

    A missing root is an empty workspace (nothing written yet). A root that
    exists but cannot be listed is reported as ``unreadable`` — the caller
    decides policy, and :class:`WorkspaceEnforcer` treats it as a violation.
    """
    usage = WorkspaceUsage()
    if not root:
        return usage
    root_real = os.path.realpath(root)
    if not os.path.isdir(root_real):
        # Missing → empty. A file where a directory belongs is unreadable.
        if os.path.lexists(root_real):
            usage.unreadable += 1
        return usage

    seen_inodes: set[tuple[int, int]] = set()
    stack: list[tuple[str, int]] = [(root_real, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            usage.unreadable += 1
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    usage.symlinks += 1
                    # Never follow: resolve only to decide whether the link
                    # points outside the boundary. Dangling links that point
                    # outside are counted as escapes too.
                    if contained_path(entry.path, root_real) is None:
                        usage.escaped_symlinks += 1
                    usage.max_depth = max(usage.max_depth, depth + 1)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    usage.directories += 1
                    child_depth = depth + 1
                    usage.max_depth = max(usage.max_depth, child_depth)
                    stack.append((entry.path, child_depth))
                    continue
                if entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    if not st.st_ino:
                        # Windows returns the FindFirstFile data here, which
                        # carries no inode (st_ino == st_nlink == 0). Pay for
                        # one real stat so hardlink identity is available.
                        try:
                            st = os.stat(entry.path, follow_symlinks=False)
                        except OSError:
                            usage.unreadable += 1
                            continue
                    # Hardlink dedupe keys on the inode, not on st_nlink:
                    # Windows reports st_nlink == 1 for every file even when
                    # hardlinks exist, so gating on it would miss them. An
                    # inode number of 0 (filesystems that cannot provide one)
                    # is never used as an identity.
                    key = (st.st_dev, st.st_ino)
                    if st.st_ino:
                        if key in seen_inodes:
                            usage.files += 1
                            usage.max_depth = max(usage.max_depth, depth + 1)
                            continue
                        seen_inodes.add(key)
                    usage.files += 1
                    usage.total_bytes += st.st_size
                    usage.largest_file_bytes = max(usage.largest_file_bytes,
                                                   st.st_size)
                    usage.max_depth = max(usage.max_depth, depth + 1)
                    continue
                # FIFO, socket, device: counted, never opened.
                usage.special_files += 1
                usage.max_depth = max(usage.max_depth, depth + 1)
            except OSError:
                usage.unreadable += 1
    return usage


class WorkspaceQuotaExceeded(Exception):
    """A refused write/run, with the dimension that refused it.

    ``kind`` is one of ``outside`` (path resolves outside the scope),
    ``file`` (single write too large), ``total`` (workspace would exceed
    its byte ceiling), ``entries``, ``depth``, ``symlink_escape``
    (the scope contains a link pointing outside), ``indeterminate``
    (a subtree could not be measured) or ``budget`` (the cumulative
    execution budget's disk dimension is exhausted).
    """

    def __init__(self, kind: str, *, scope: str, used: float = 0,
                 limit: float = 0, detail: str = "") -> None:
        self.kind = kind
        self.scope = scope
        self.used = used
        self.limit = limit
        self.detail = detail
        super().__init__(self.__str__())

    def __str__(self) -> str:
        head = f"workspace quota refused ({self.kind})"
        if self.detail:
            head += f": {self.detail}"
        if self.limit:
            head += f" [used {self.used:.0f}, limit {self.limit:.0f}]"
        return head + f" in {self.scope}"

    def status(self) -> dict:
        return {"kind": self.kind, "scope": self.scope,
                "used": self.used, "limit": self.limit,
                "detail": self.detail}


class WorkspaceQuotaCache:
    """One :class:`WorkspaceEnforcer` per resolved scope, for long-lived adapters.

    Adapters are constructed once but serve many task scopes (the scope is
    derived from the kernel context per call), so the enforcer — and its
    measurement baseline — must be resolved per scope, not per adapter.
    """

    def __init__(self, quota: WorkspaceQuota, budget=None) -> None:
        self._quota = quota
        self._budget = budget
        self._lock = threading.Lock()
        self._by_scope: dict[str, WorkspaceEnforcer] = {}

    @property
    def quota(self) -> WorkspaceQuota:
        return self._quota

    def for_scope(self, scope: str) -> WorkspaceEnforcer:
        key = os.path.normcase(os.path.realpath(scope))
        with self._lock:
            enforcer = self._by_scope.get(key)
            if enforcer is None:
                enforcer = WorkspaceEnforcer(scope, self._quota,
                                             budget=self._budget)
                self._by_scope[key] = enforcer
            return enforcer

    def snapshot(self) -> dict[str, dict]:
        """Per-scope usage, keyed by the scope path as configured."""
        with self._lock:
            enforcers = list(self._by_scope.values())
        return {enf.root: enf.usage.status() for enf in enforcers}


class WorkspaceEnforcer:
    """Pre-write enforcement plus pre/post-run gates for one workspace scope."""

    def __init__(self, root: str, quota: WorkspaceQuota,
                 budget=None) -> None:
        self._root = os.path.abspath(root)
        self._root_real = os.path.realpath(root)
        self._quota = quota
        self._budget = budget
        self._lock = threading.RLock()
        self._pending_bytes = 0
        self._pending_entries = 0
        self._usage: WorkspaceUsage | None = None
        self._last_recorded_bytes = 0

    # -- inspection ----------------------------------------------------

    @property
    def root(self) -> str:
        return self._root

    @property
    def quota(self) -> WorkspaceQuota:
        return self._quota

    def refresh(self) -> WorkspaceUsage:
        usage = measure_workspace(self._root_real)
        with self._lock:
            self._usage = usage
        return usage

    @property
    def usage(self) -> WorkspaceUsage:
        with self._lock:
            cached = self._usage
        return cached if cached is not None else self.refresh()

    # -- internal policy evaluation ------------------------------------

    def _violation(self, usage: WorkspaceUsage
                   ) -> tuple[str, float, float, str] | None:
        q = self._quota
        if not q.allow_escaped_symlinks and usage.escaped_symlinks:
            return ("symlink_escape",
                    float(usage.escaped_symlinks), 0.0,
                    f"{usage.escaped_symlinks} symlink(s) resolve outside the workspace")
        if usage.unreadable:
            return ("indeterminate",
                    float(usage.unreadable), 0.0,
                    f"{usage.unreadable} path(s) could not be measured; refusing to "
                    "reason about a workspace whose bytes cannot be counted")
        if q.max_total_bytes is not None and usage.total_bytes > q.max_total_bytes:
            return ("total", float(usage.total_bytes), float(q.max_total_bytes),
                    "workspace exceeds its total byte quota")
        if q.max_entries is not None and usage.entries > q.max_entries:
            return ("entries", float(usage.entries), float(q.max_entries),
                    "workspace exceeds its entry quota")
        if q.max_depth is not None and usage.max_depth > q.max_depth:
            return ("depth", float(usage.max_depth), float(q.max_depth),
                    "workspace exceeds its directory-depth quota")
        return None

    def _raise(self, violation: tuple[str, float, float, str]) -> None:
        kind, used, limit, detail = violation
        raise WorkspaceQuotaExceeded(kind, scope=self._root, used=used,
                                     limit=limit, detail=detail)

    @staticmethod
    def _depth_of(path: str, root_real: str) -> int:
        rel = os.path.relpath(os.path.abspath(path), root_real)
        if rel in (os.curdir, ""):
            return 0
        return len([p for p in rel.split(os.sep) if p not in ("", os.curdir)])

    def _missing_ancestors(self, dest: str) -> int:
        """Directories that would be CREATED for ``dest`` (they are entries too).

        Stops at the workspace root: ancestors above it are the caller's
        business, not this scope's entries.
        """
        missing = 0
        parent = os.path.dirname(os.path.abspath(dest))
        while parent and not os.path.lexists(parent):
            if os.path.normcase(parent) == os.path.normcase(self._root_real):
                break
            missing += 1
            nxt = os.path.dirname(parent)
            if nxt == parent:
                break
            parent = nxt
        return missing

    # -- gates ---------------------------------------------------------

    def check_before_run(self) -> None:
        """Refuse a run whose workspace is already over quota or unmeasurable.

        Also sets the baseline the post-run measurement reports growth
        against, so a shared cumulative budget sees each run's growth once.
        """
        usage = self.refresh()
        with self._lock:
            self._last_recorded_bytes = usage.total_bytes
        if self._budget is not None:
            self._check_budget_disk()
        violation = self._violation(usage)
        if violation is not None:
            self._raise(violation)

    def check_write(self, dest: str, size: int, *,
                    replace_bytes: int | None = None) -> None:
        """Refuse a write BEFORE any bytes land. Raises ``WorkspaceQuotaExceeded``."""
        if size < 0:
            raise ValueError("size must be non-negative")
        if contained_path(dest, self._root_real) is None:
            raise WorkspaceQuotaExceeded(
                "outside", scope=self._root,
                detail=f"{dest!r} resolves outside the workspace",
            )
        q = self._quota
        if q.max_file_bytes is not None and size > q.max_file_bytes:
            raise WorkspaceQuotaExceeded(
                "file", scope=self._root, used=float(size),
                limit=float(q.max_file_bytes),
                detail=f"single write of {size} bytes exceeds the per-file quota",
            )
        depth = self._depth_of(dest, self._root_real)
        if q.max_depth is not None and depth > q.max_depth:
            raise WorkspaceQuotaExceeded(
                "depth", scope=self._root, used=float(depth),
                limit=float(q.max_depth),
                detail=f"path depth {depth} exceeds the depth quota",
            )

        with self._lock:
            usage = self.refresh()
            violation = self._violation(usage)
            if violation is not None:
                self._raise(violation)
            if self._budget is not None:
                self._check_budget_disk()

            if replace_bytes is None:
                try:
                    st = os.lstat(os.path.abspath(dest))
                    replace_bytes = st.st_size if not os.path.islink(
                        os.path.abspath(dest)) else 0
                except OSError:
                    replace_bytes = 0
            replaced = max(0, int(replace_bytes))

            if q.max_entries is not None:
                new_entries = 0
                if not os.path.lexists(os.path.abspath(dest)):
                    new_entries += 1
                new_entries += self._missing_ancestors(dest)
                projected_entries = (usage.entries + self._pending_entries
                                     + new_entries)
                if projected_entries > q.max_entries:
                    raise WorkspaceQuotaExceeded(
                        "entries", scope=self._root,
                        used=float(projected_entries),
                        limit=float(q.max_entries),
                        detail="write would exceed the workspace entry quota",
                    )

            if q.max_total_bytes is not None:
                projected = (usage.total_bytes - replaced
                             + self._pending_bytes + size)
                if projected > q.max_total_bytes:
                    raise WorkspaceQuotaExceeded(
                        "total", scope=self._root, used=float(projected),
                        limit=float(q.max_total_bytes),
                        detail=("write would exceed the workspace byte quota "
                                f"(existing {usage.total_bytes}, replaced "
                                f"{replaced}, pending {self._pending_bytes}, "
                                f"incoming {size})"),
                    )

    def _check_budget_disk(self) -> None:
        if self._budget is None:
            return
        limit = getattr(self._budget, "disk_bytes", None)
        used = getattr(self._budget, "disk_used", 0.0)
        if limit is not None and used >= limit:
            raise WorkspaceQuotaExceeded(
                "budget", scope=self._root, used=used, limit=limit,
                detail="cumulative execution budget disk dimension exhausted",
            )

    @contextlib.contextmanager
    def reserve(self, dest: str, size: int, *,
                replace_bytes: int | None = None) -> Iterator[None]:
        """Check, then hold a reservation for the duration of the write.

        Concurrent writers in this process cannot collectively overshoot:
        each holds its bytes in ``_pending_bytes`` until the write returns.
        The reservation is released even when the write raises.
        """
        self.check_write(dest, size, replace_bytes=replace_bytes)
        created = 1 if not os.path.lexists(os.path.abspath(dest)) else 0
        created += self._missing_ancestors(dest)
        with self._lock:
            self._pending_bytes += size
            self._pending_entries += created
        try:
            yield
        finally:
            with self._lock:
                self._pending_bytes = max(0, self._pending_bytes - size)
                self._pending_entries = max(0, self._pending_entries - created)

    def record_after_run(self) -> WorkspaceQuotaExceeded | None:
        """Measure after an execution; record growth; return a violation if any.

        Returns rather than raises because the child has already exited —
        the caller reports the violation in the result (``limits_hit``)
        instead of pretending the run was quota-clean.
        """
        with self._lock:
            usage = self.refresh()
            grew = max(0, usage.total_bytes - self._last_recorded_bytes)
            self._last_recorded_bytes = usage.total_bytes
            budget = self._budget
        if budget is not None and grew:
            try:
                budget.record_usage(disk_bytes=float(grew))
            except TypeError:  # budget implementation without a disk dimension
                pass
        violation = self._violation(usage)
        if violation is None:
            return None
        kind, used, limit, detail = violation
        return WorkspaceQuotaExceeded(kind, scope=self._root, used=used,
                                      limit=limit, detail=detail)
