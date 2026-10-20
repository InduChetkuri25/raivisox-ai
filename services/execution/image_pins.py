"""Immutable image pinning for the container backend — supply chain (V2 closeout).

The container gate used to name its image by tag (``python:3.12-slim``). A
tag is a mutable pointer: a retagged or re-pushed image changes what "the
same" acceptance run actually executed, so a green gate could silently
certify different bytes than the ones reviewed.

Policy implemented here:

- A run may only use an image resolved to an **immutable digest**
  (``repo:tag@sha256:<64 hex>``). The digest is what the runtime pulls and
  what the container runs — not the tag.
- Approved tag→digest pairs live in :data:`APPROVED_IMAGES`. A configured
  digest that contradicts the approved digest for the same tag is refused
  (that is either a typo or a substitution attempt; either way, fail).
- An unapproved tag with no digest is refused by default
  (``ImagePinError``) — there is no silent "pull :latest" path.
  ``SANDBOX_ALLOW_UNPINNED_IMAGE=1`` is the explicit, documented
  development escape hatch; when it is used the sandbox reports
  ``image_pinned: False``, so the weakening is visible in the result
  rather than hidden.
- Verification happens against the runtime BEFORE the container runs:
  pull the digest-qualified reference, then require that exact digest to
  appear in the local image's ``RepoDigests``. A pull failure, a missing
  image, an unreadable inspect result, or a digest mismatch is an honest
  fail-closed error — the run does not happen.

Updating an approved digest is a reviewed change, not an automatic one:

1. Resolve the new digest from the registry manifest list, e.g.
   ``curl -s https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim``
   and read the top-level ``digest`` field (NOT the per-architecture ones).
2. Update the entry in :data:`APPROVED_IMAGES` **and** the
   ``SANDBOX_CONTAINER_IMAGE`` value in ``.github/workflows/ci.yml`` in the
   same commit, with the resolution date in the comment.
3. Run the live container gate. The workflow pins the same digest, and
   ``tests/unit/test_image_pins.py`` asserts the two agree, so a
   half-updated pin fails locally before it can waste a CI run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

__all__ = [
    "APPROVED_IMAGES",
    "ImageRef",
    "ImagePinError",
    "parse_image_ref",
    "resolve_pinned_ref",
    "verify_image",
    "reset_verification_cache",
    "allow_unpinned_images",
]

_HEX_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# Deliberately strict: registry/repository characters only, and it must
# START with an alphanumeric so a reference can never be parsed by the
# runtime CLI as a flag (e.g. "--privileged").
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")

APPROVED_IMAGES: dict[str, str] = {
    # Resolved 2026-10-20 from the Docker Hub manifest list for the tag.
    "python:3.12-slim":
        "sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1",
}

_UNPINNED_ENV = "SANDBOX_ALLOW_UNPINNED_IMAGE"
_VERIFIED: set[tuple[str, str]] = set()


class ImagePinError(Exception):
    """The configured image cannot be resolved to an approved immutable digest."""


@dataclass(frozen=True)
class ImageRef:
    """A parsed container image reference."""

    reference: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def approval_key(self) -> str:
        """``repository:tag`` — the key used by :data:`APPROVED_IMAGES`."""
        return f"{self.repository}:{self.tag or 'latest'}"

    @property
    def is_pinned(self) -> bool:
        return self.digest is not None

    @property
    def pinned(self) -> str:
        """Digest-qualified reference (raises when no digest is present)."""
        if self.digest is None:
            raise ImagePinError(f"{self.reference!r} carries no digest")
        return f"{self.repository}@{self.digest}"


def allow_unpinned_images() -> bool:
    """Explicit development override — reported, never assumed."""
    return os.environ.get(_UNPINNED_ENV, "").strip().lower() in ("1", "true", "yes")


def parse_image_ref(reference: str) -> ImageRef:
    """Parse ``[registry/]repo[:tag][@sha256:<64 hex>]`` strictly.

    Rejects anything a runtime CLI could misinterpret as a flag, anything
    with whitespace or a URL scheme, malformed digests, and empty parts.
    """
    if not isinstance(reference, str):
        raise ImagePinError("image reference must be a string")
    raw = reference.strip()
    if not raw:
        raise ImagePinError("image reference is empty")
    if raw.startswith("-"):
        raise ImagePinError(
            f"image reference {raw!r} starts with '-' and would be parsed as a flag"
        )
    if "://" in raw:
        raise ImagePinError(f"image reference {raw!r} must not contain a URL scheme")
    if not _SAFE_REFERENCE.match(raw):
        raise ImagePinError(
            f"image reference {raw!r} contains characters outside the allowed set"
        )

    body, _, digest = raw.partition("@")
    if "@" in digest:
        raise ImagePinError(f"image reference {raw!r} has more than one '@'")
    digest = digest or None
    if digest is not None and not _HEX_DIGEST.match(digest):
        raise ImagePinError(
            f"image digest {digest!r} must be 'sha256:' followed by 64 lowercase hex chars"
        )

    # A tag exists only when a ':' appears after the last '/'.
    last_slash = body.rfind("/")
    last_colon = body.rfind(":")
    repository, tag = body, None
    if last_colon > last_slash:
        repository, tag = body[:last_colon], body[last_colon + 1:]
        if not tag:
            raise ImagePinError(f"image reference {raw!r} has an empty tag")
    if not repository:
        raise ImagePinError(f"image reference {raw!r} has an empty repository")
    if repository.startswith("/") or repository.endswith("/"):
        raise ImagePinError(f"image reference {raw!r} has an empty path segment")
    return ImageRef(reference=raw, repository=repository, tag=tag, digest=digest)


def resolve_pinned_ref(reference: str, *,
                       allow_unpinned: bool | None = None
                       ) -> tuple[str | None, str]:
    """Resolve a reference to an immutable one.

    Returns ``(pinned_reference, note)`` on success, ``(None, reason)`` when
    the policy refuses it. A refusal is never downgraded to a warning.
    """
    try:
        ref = parse_image_ref(reference)
    except ImagePinError as exc:
        return None, str(exc)

    approved = APPROVED_IMAGES.get(ref.approval_key)
    if ref.digest is not None:
        if approved is not None and approved != ref.digest:
            return None, (
                f"configured digest for {ref.approval_key} does not match the "
                f"approved digest (approved {approved}, configured {ref.digest}; "
                "update APPROVED_IMAGES and the CI workflow together if the "
                "image is intentionally moving)"
            )
        return ref.pinned, ("approved digest" if approved else "explicit digest")

    if approved is not None:
        return f"{ref.repository}@{approved}", f"approved tag resolved to {approved}"

    override = allow_unpinned_images() if allow_unpinned is None else allow_unpinned
    if override:
        return ref.reference, (
            f"UNPINNED image {ref.reference!r} allowed by {_UNPINNED_ENV}; "
            "the gate cannot certify which bytes ran"
        )
    return None, (
        f"image {ref.reference!r} is not pinned to an immutable digest and "
        f"{ref.approval_key!r} is not in APPROVED_IMAGES; configure "
        "'repo:tag@sha256:<digest>' or add an approved entry (set "
        f"{_UNPINNED_ENV}=1 only for local development)"
    )


def reset_verification_cache() -> None:
    """Forget verified (runtime, pinned-ref) pairs — tests and digest rotation."""
    _VERIFIED.clear()


def _last_line(output: bytes) -> str:
    text = output.decode(errors="replace").strip().splitlines()
    return text[-1][:300] if text else ""


def _run(argv: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, trusted runtime binary
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        check=False,
    )


def verify_image(runtime_exe: str, reference: str, *, pull: bool = True,
                 timeout_s: float = 300.0) -> tuple[str | None, str]:
    """Pull + verify an image, returning the digest-qualified reference to run.

    ``(pinned_ref, "")`` on success; ``(None, reason)`` when the image
    cannot be resolved, pulled, inspected, or matched. Synchronous — async
    callers run it in a worker thread. Successful verifications are cached
    per process so a gate run verifies once, not once per execution.
    """
    pinned, note = resolve_pinned_ref(reference)
    if pinned is None:
        return None, f"image pinning refused: {note}"
    if "@" not in pinned:  # explicit unpinned override — nothing to verify
        return pinned, ""

    key = (runtime_exe, pinned)
    if key in _VERIFIED:
        return pinned, ""

    if pull:
        try:
            pulled = _run([runtime_exe, "pull", pinned], timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"image pull failed for {pinned}: {type(exc).__name__}"
        if pulled.returncode != 0:
            return None, (f"image pull failed for {pinned} "
                          f"(exit {pulled.returncode}): {_last_line(pulled.stderr)}")

    try:
        inspected = _run(
            [runtime_exe, "image", "inspect", "--format", "{{json .RepoDigests}}",
             pinned],
            timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"image verification failed for {pinned}: {type(exc).__name__}"
    if inspected.returncode != 0:
        return None, (f"image not present after pull: {pinned} "
                      f"(exit {inspected.returncode}): {_last_line(inspected.stderr)}")

    try:
        repo_digests = json.loads(inspected.stdout.decode() or "[]")
    except (ValueError, UnicodeDecodeError):
        return None, f"image inspect output for {pinned} was not valid JSON"
    if not isinstance(repo_digests, list):
        return None, f"image inspect output for {pinned} had an unexpected shape"

    expected = pinned.split("@", 1)[1]
    found = {d.rsplit("@", 1)[1] for d in repo_digests
             if isinstance(d, str) and "@" in d}
    if expected not in found:
        return None, (
            f"pulled image does not match the pinned digest (expected {expected}, "
            f"local RepoDigests {sorted(found) or 'none'})"
        )

    _VERIFIED.add(key)
    return pinned, ""
