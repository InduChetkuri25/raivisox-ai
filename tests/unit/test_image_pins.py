"""Image pinning — reference parsing, digest policy, verification, fail-closed.

Covers the V2 closeout supply-chain control (``services/execution/image_pins.py``):

- strict reference parsing (malformed digests, flags, schemes, empty parts)
- approved tag→digest resolution; a contradicting digest is refused
- an unapproved, unpinned tag is refused by default; the documented dev
  override allows it AND is reported as not pinned
- verification: pull by digest, ``RepoDigests`` must contain that digest;
  pull failure, inspect failure, invalid output, wrong shape, missing
  digest, and digest mismatch are all honest failures
- the runtime CLI is driven with argv lists only (no shell), and the
  reference is a single argument that can never be read as a flag
- the container backend refuses to run when verification fails, runs the
  DIGEST-qualified reference when it succeeds, and reports ``image_pinned``
  honestly
- CI pins the same digest the approved table names (a half-updated pin
  fails here instead of wasting a live gate run)
"""

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from services.execution import container_sandbox as cs
from services.execution.image_pins import (
    APPROVED_IMAGES,
    ImagePinError,
    allow_unpinned_images,
    parse_image_ref,
    reset_verification_cache,
    resolve_pinned_ref,
    verify_image,
)
from services.execution.sandbox import SandboxSpec

APPROVED_TAG = "python:3.12-slim"
APPROVED_DIGEST = APPROVED_IMAGES[APPROVED_TAG]
APPROVED_REF = f"python@{APPROVED_DIGEST}"


def _completed(argv, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    reset_verification_cache()
    monkeypatch.delenv("SANDBOX_ALLOW_UNPINNED_IMAGE", raising=False)
    yield
    reset_verification_cache()


def _spec(cwd="/tmp") -> SandboxSpec:
    return SandboxSpec(argv=["true"], cwd=cwd, env={}, timeout_s=5,
                       workspace_writable=cwd)


# ---------------------------------------------------------------------------
# 1. Parsing
# ---------------------------------------------------------------------------

def test_parses_registry_tag_and_digest():
    ref = parse_image_ref(
        "ghcr.io/org/app:1.2.3@sha256:" + "a" * 64)
    assert ref.repository == "ghcr.io/org/app"
    assert ref.tag == "1.2.3"
    assert ref.digest == "sha256:" + "a" * 64
    assert ref.approval_key == "ghcr.io/org/app:1.2.3"
    assert ref.pinned == "ghcr.io/org/app@sha256:" + "a" * 64


def test_parses_tag_without_digest():
    ref = parse_image_ref("python:3.12-slim")
    assert ref.tag == "3.12-slim"
    assert ref.digest is None
    assert ref.is_pinned is False
    assert ref.approval_key == "python:3.12-slim"


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "--privileged",
    "python@sha256:short",
    "python@sha256:" + "A" * 64,          # uppercase hex
    "python@sha256:" + "g" * 64,          # non-hex
    "python@md5:" + "a" * 64,             # wrong algorithm
    "https://registry/python:3.12",
    "python:@sha256:" + "a" * 64,         # empty tag
    "@sha256:" + "a" * 64,                # empty repository
    "/python:3.12",
    "python:3.12@sha256:" + "a" * 64 + "@x",
    "python 3.12",
])
def test_rejects_malformed_references(bad):
    with pytest.raises(ImagePinError):
        parse_image_ref(bad)


# ---------------------------------------------------------------------------
# 2. Policy: approved digests, refusal, and the dev override
# ---------------------------------------------------------------------------

def test_approved_tag_resolves_to_its_digest():
    pinned, note = resolve_pinned_ref(APPROVED_TAG)
    assert pinned == APPROVED_REF
    assert str(APPROVED_DIGEST) in note


def test_explicit_approved_digest_is_accepted():
    pinned, note = resolve_pinned_ref(f"{APPROVED_TAG}@{APPROVED_DIGEST}")
    assert pinned == APPROVED_REF
    assert note == "approved digest"


def test_unapproved_but_digest_pinned_reference_is_accepted():
    digest = "sha256:" + "b" * 64
    pinned, note = resolve_pinned_ref(f"alpine:3.20@sha256:{'b' * 64}")
    assert pinned == f"alpine@{digest}"
    assert note == "explicit digest"


def test_contradicting_digest_for_an_approved_tag_is_refused():
    wrong = "sha256:" + "c" * 64
    pinned, reason = resolve_pinned_ref(f"{APPROVED_TAG}@{wrong}")
    assert pinned is None
    assert "does not match the approved digest" in reason
    assert APPROVED_DIGEST in reason and wrong in reason


def test_unpinned_unapproved_tag_is_refused_by_default():
    pinned, reason = resolve_pinned_ref("alpine:3.20")
    assert pinned is None
    assert "not pinned to an immutable digest" in reason
    assert "SANDBOX_ALLOW_UNPINNED_IMAGE" in reason


def test_unpinned_override_is_explicit_and_flagged(monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOW_UNPINNED_IMAGE", "1")
    assert allow_unpinned_images() is True
    pinned, note = resolve_pinned_ref("alpine:3.20")
    assert pinned == "alpine:3.20"
    assert "UNPINNED" in note


def test_override_does_not_bypass_a_digest_mismatch(monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOW_UNPINNED_IMAGE", "1")
    pinned, reason = resolve_pinned_ref(
        f"{APPROVED_TAG}@sha256:{'d' * 64}")
    assert pinned is None
    assert "does not match the approved digest" in reason


# ---------------------------------------------------------------------------
# 3. Verification (pull + RepoDigests)
# ---------------------------------------------------------------------------

def test_verify_pulls_the_digest_reference_and_checks_repo_digests(monkeypatch):
    calls = []

    def fake_run(argv, timeout_s):
        calls.append(list(argv))
        if argv[1] == "pull":
            return _completed(argv)
        return _completed(argv, stdout=json.dumps(
            [f"python@{APPROVED_DIGEST}"]).encode())

    monkeypatch.setattr("services.execution.image_pins._run", fake_run)
    pinned, err = verify_image("/usr/bin/docker", APPROVED_TAG)

    assert (pinned, err) == (APPROVED_REF, "")
    assert calls[0] == ["/usr/bin/docker", "pull", APPROVED_REF]
    assert calls[1][:5] == ["/usr/bin/docker", "image", "inspect", "--format",
                            "{{json .RepoDigests}}"]
    assert calls[1][-1] == APPROVED_REF


def test_verification_is_cached_per_runtime_and_ref(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.execution.image_pins._run",
        lambda argv, timeout_s: (calls.append(list(argv)),
                                 _completed(argv, stdout=json.dumps(
                                     [f"python@{APPROVED_DIGEST}"]).encode()))[1],
    )
    verify_image("/usr/bin/docker", APPROVED_TAG)
    verify_image("/usr/bin/docker", APPROVED_TAG)
    assert len(calls) == 2          # one pull + one inspect, then cached


def test_pull_failure_fails_closed(monkeypatch):
    monkeypatch.setattr("services.execution.image_pins._run", lambda argv,
                        timeout_s: _completed(
                            argv, returncode=1,
                            stderr=b"Error response from daemon: manifest unknown"))
    pinned, err = verify_image("/usr/bin/docker", APPROVED_TAG)
    assert pinned is None
    assert "image pull failed" in err
    assert "manifest unknown" in err


def test_inspect_failure_fails_closed(monkeypatch):
    def fake_run(argv, timeout_s):
        if argv[1] == "pull":
            return _completed(argv)
        return _completed(argv, returncode=1, stderr=b"no such image")

    monkeypatch.setattr("services.execution.image_pins._run", fake_run)
    pinned, err = verify_image("/usr/bin/docker", APPROVED_TAG)
    assert pinned is None
    assert "image not present after pull" in err


def test_invalid_inspect_json_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "services.execution.image_pins._run",
        lambda argv, timeout_s: _completed(argv, stdout=b"not-json"))
    pinned, err = verify_image("/usr/bin/docker", APPROVED_TAG)
    assert pinned is None
    assert "not valid JSON" in err


def test_unexpected_inspect_shape_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "services.execution.image_pins._run",
        lambda argv, timeout_s: _completed(argv, stdout=b'{"digest": "x"}'))
    pinned, err = verify_image("/usr/bin/docker", APPROVED_TAG)
    assert pinned is None
    assert "unexpected shape" in err


def test_digest_mismatch_after_pull_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "services.execution.image_pins._run",
        lambda argv, timeout_s: _completed(argv, stdout=json.dumps(
            ["python@sha256:" + "e" * 64]).encode()))
    pinned, err = verify_image("/usr/bin/docker", APPROVED_TAG)
    assert pinned is None
    assert "does not match the pinned digest" in err


def test_unpinned_override_verifies_nothing_and_says_so(monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOW_UNPINNED_IMAGE", "1")

    def explode(argv, timeout_s):       # must not be called
        raise AssertionError("no pull may happen for an unpinned override")

    monkeypatch.setattr("services.execution.image_pins._run", explode)
    pinned, err = verify_image("/usr/bin/docker", "alpine:3.20")
    assert pinned == "alpine:3.20"
    assert err == ""


def test_policy_refusal_never_reaches_the_runtime(monkeypatch):
    def explode(argv, timeout_s):
        raise AssertionError("runtime must not be touched when policy refuses")

    monkeypatch.setattr("services.execution.image_pins._run", explode)
    pinned, err = verify_image("/usr/bin/docker", "alpine:3.20")
    assert pinned is None
    assert "image pinning refused" in err


def test_runtime_cli_is_driven_with_argv_lists_only(monkeypatch):
    seen = {}

    def fake_subprocess_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _completed(argv, stdout=json.dumps(
            [f"python@{APPROVED_DIGEST}"]).encode())

    monkeypatch.setattr("services.execution.image_pins.subprocess.run",
                        fake_subprocess_run)
    verify_image("/usr/bin/docker", APPROVED_TAG)

    assert isinstance(seen["argv"], list)
    assert seen["kwargs"].get("shell") is None       # never a shell string
    assert APPROVED_REF in seen["argv"]
    assert seen["argv"][1] == "image"                # inspect, not a joined string
    assert not any(" " in part for part in seen["argv"][:3])


# ---------------------------------------------------------------------------
# 4. Backend behaviour: refuse, or run the digest
# ---------------------------------------------------------------------------

class _FakeStream:
    """Minimal stand-in for the CLI's stdout/stderr pipe."""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self, limit: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data if limit < 0 else data[:limit]


class _FakeProc:
    def __init__(self, argv, stdout=b"ok", stderr=b"", rc=0):
        self.argv = argv
        self.returncode = rc
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)

    async def communicate(self):
        return await self.stdout.read(), await self.stderr.read()

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _patch_runtime(monkeypatch, exe="/usr/bin/docker"):
    monkeypatch.setattr(cs, "probe_runtime", lambda force=False: (exe, ""))


async def test_backend_refuses_when_image_verification_fails(tmp_path,
                                                             monkeypatch):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(cs, "verify_image",
                        lambda exe, ref: (None, "manifest unknown"))
    spawned = []

    async def fake_spawn(*argv, **kwargs):
        spawned.append(argv)
        return _FakeProc(argv)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    result = await cs.ContainerSandbox(image=APPROVED_TAG).run(_spec(str(tmp_path)))

    assert result.success is False
    assert result.killed_reason == "supervisor"
    assert "sandbox image verification failed" in (result.error or "")
    assert "manifest unknown" in (result.error or "")
    assert spawned == []                # no container, no pull-through-run


async def test_backend_runs_the_digest_qualified_reference(tmp_path, monkeypatch):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(cs, "verify_image", lambda exe, ref: (APPROVED_REF, ""))
    spawned = []

    async def fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _FakeProc(argv)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    sandbox = cs.ContainerSandbox(image=APPROVED_TAG)
    result = await sandbox.run(_spec(str(tmp_path)))

    assert result.success is True, result.error
    assert spawned, "a container run must have been attempted"
    assert spawned[0][1] == "run"
    assert APPROVED_REF in spawned[0]
    assert APPROVED_TAG not in spawned[0]      # the tag never reaches docker
    assert "image_pinned" in result.enforced


def test_capabilities_report_pinning_honestly(monkeypatch):
    _patch_runtime(monkeypatch)
    assert cs.ContainerSandbox(image=APPROVED_TAG).capabilities()["image_pinned"] is True
    assert cs.ContainerSandbox(image=APPROVED_TAG).capabilities()["workspace_quota"] is False

    # Unapproved + unpinned: not pinned, and the override stays honest.
    assert cs.ContainerSandbox(image="alpine:3.20").capabilities()["image_pinned"] is False
    monkeypatch.setenv("SANDBOX_ALLOW_UNPINNED_IMAGE", "1")
    assert cs.ContainerSandbox(image="alpine:3.20").capabilities()["image_pinned"] is False


# ---------------------------------------------------------------------------
# 5. CI pins the approved digest
# ---------------------------------------------------------------------------

def test_ci_workflow_pins_the_approved_digest():
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")
    match = re.search(r"SANDBOX_CONTAINER_IMAGE:\s*(\S+)", text)
    assert match, "the container gate must configure SANDBOX_CONTAINER_IMAGE"
    configured = match.group(1).strip().strip('"\'')

    ref = parse_image_ref(configured)
    assert ref.is_pinned, f"CI image {configured!r} is not digest-pinned"
    assert f"{ref.repository}:{ref.tag}" in APPROVED_IMAGES
    assert APPROVED_IMAGES[f"{ref.repository}:{ref.tag}"] == ref.digest
