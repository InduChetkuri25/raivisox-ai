"""Symlink-resolving path containment — the shared workspace boundary.

SECURITY (Phase 0 of the V2 sandbox design, docs/sandbox-design.md):
workspace clamping previously decided containment on
``normcase(abspath(path))``, which does NOT resolve symlinks, while
artifact registration decided it on ``realpath``. A symlink planted
inside a task scope could therefore redirect reads and writes outside
the workspace root even though every prefix check "passed".

Every consumer that enforces workspace containment MUST go through
``contained_path`` here so the boundary resolves symlinks the same way
everywhere — before deciding whether a path is inside the root.

Documented behavior:
- Paths whose real resolution lands inside the root are allowed; the
  ORIGINAL (pre-resolution) path is returned so callers keep writing
  through their intended relative location.
- Paths that resolve outside the root (``..`` traversal, absolute paths
  outside, symlinks pointing outside) are rejected: ``None``.
- Missing paths are resolved over the existing portion of the parent
  chain (``resolve(strict=False)`` semantics). A write target whose
  existing parents resolve inside the root is allowed — creating a new
  file cannot escape; a symlink created later is re-checked at use time
  by the tool-level checks (file_reader/file_writer call this again).
- On Windows, symlinks require privileges to CREATE but are resolved
  transparently by the OS; ``os.path.resolve`` behaves the same, so the
  check is uniform across platforms. Unresolvable paths are denied.
"""

from __future__ import annotations

import os

__all__ = ["contained_path", "realpath_norm", "is_contained"]


def realpath_norm(path: str) -> str:
    """Fully resolved, case-normalized absolute path (symlinks resolved)."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_contained(path: str, root: str) -> bool:
    """True when ``path``'s real resolution lies inside ``root``.

    ``root`` itself is contained (callers may address the scope root
    directly); everything strictly outside is not.
    """
    p = realpath_norm(path)
    r = realpath_norm(root)
    return p == r or p.startswith(r + os.sep)


def contained_path(path: str, root: str) -> str | None:
    """Return ``path`` if it is contained in ``root``, else ``None``.

    This is the single containment decision used by every workspace
    clamp and by the file tool allowlist checks. The returned value is
    the ORIGINAL path (absolute or workspace-relative as given), not the
    resolved one, so callers' writes land where they intended — provided
    the real resolution stayed inside the boundary.
    """
    if not path or not root:
        return None
    return path if is_contained(path, root) else None
