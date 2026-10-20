"""Workspace write quotas — enforcement, boundaries, and bypass attempts.

Covers the V2 closeout T8 control (``services/execution/workspace_quota.py``):

- measurement: nested trees, directories as entries, depth, hardlink
  dedupe, symlinks never followed, escaped links flagged, unreadable
  subtrees flagged, special files never opened
- pre-write refusal: per-file, total (with replace credit), entry count
  (including directories a write would create), depth, outside-scope
  destination, symlinked parent escaping the scope, escaped link already
  present, unmeasurable workspace, exhausted disk budget
- bypass attempts: absolute path outside, ``..`` traversal, symlink to a
  sibling tree, oversized payload, many small files, deep nesting,
  concurrent reservations that would collectively overshoot
- cleanup: deleting files reclaims quota; a refused write leaves no bytes
  and no reservation behind
- integration: ``fs_write`` refuses before writing, an execution whose
  child overfills the workspace is reported honestly (never a rewritten
  exit code), and a run is refused outright when the scope is already over

The distinction this module keeps in view: these are WORKSPACE WRITE
quotas. A filesystem-wide disk quota is a different mechanism and is not
claimed here (see the module docstring of workspace_quota).
"""

import os
import sys

import pytest

from integrations.adapters.base import AdapterContext
from integrations.adapters.builtin import FileWriteAdapter
from services.execution.quotas import ExecutionBudget
from services.execution.sandbox import ProcessSandbox, SandboxSpec, WorkspaceQuota
from services.execution.workspace_quota import (
    WorkspaceEnforcer,
    WorkspaceQuotaCache,
    WorkspaceQuotaExceeded,
    measure_workspace,
)


def _ctx(task="t1", actor="tester") -> AdapterContext:
    return AdapterContext(actor_id=actor, task_id=task, step_id="s1",
                          resource=f"task:{task}")


def _write(root, rel, data: bytes | str = b"x") -> str:
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path) or str(root), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    return path


def _symlink_or_skip(target, link) -> None:
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink creation requires privileges on this host")


# ---------------------------------------------------------------------------
# 1. Measurement
# ---------------------------------------------------------------------------

def test_measures_nested_files_and_directories(tmp_path):
    _write(tmp_path, "a.txt", b"12345")
    _write(tmp_path, "sub/b.txt", b"123")
    _write(tmp_path, "sub/deeper/c.txt", b"12")

    usage = measure_workspace(str(tmp_path))

    assert usage.total_bytes == 10
    assert usage.files == 3
    assert usage.directories == 2          # "sub" and "sub/deeper"
    assert usage.entries == 5
    assert usage.max_depth == 3            # sub/deeper/c.txt
    assert usage.escaped_symlinks == 0
    assert usage.unreadable == 0


def test_missing_workspace_measures_empty(tmp_path):
    usage = measure_workspace(str(tmp_path / "does-not-exist"))
    assert usage.status() == {
        "total_bytes": 0, "files": 0, "directories": 0, "entries": 0,
        "symlinks": 0, "escaped_symlinks": 0, "special_files": 0,
        "unreadable": 0, "max_depth": 0, "largest_file_bytes": 0,
    }


def test_symlink_to_large_outside_file_is_not_followed(tmp_path):
    outside = tmp_path.parent / "outside-big.bin"
    outside.write_bytes(b"z" * 4096)
    ws = tmp_path / "ws"
    ws.mkdir()
    _symlink_or_skip(outside, ws / "big-link")

    usage = measure_workspace(str(ws))

    assert usage.total_bytes == 0        # the 4096 bytes are NOT counted
    assert usage.symlinks == 1
    assert usage.escaped_symlinks == 1   # and the escape is visible


def test_in_scope_symlink_is_counted_not_escaped(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws, "real.txt", b"1234")
    _symlink_or_skip(ws / "real.txt", ws / "alias.txt")

    usage = measure_workspace(str(ws))

    assert usage.symlinks == 1
    assert usage.escaped_symlinks == 0
    assert usage.total_bytes == 4        # counted once, through the real name


def test_hardlinked_inode_is_counted_once(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    first = _write(ws, "a.bin", b"y" * 100)
    try:
        os.link(first, str(ws / "b.bin"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hardlinks are not supported on this filesystem")

    usage = measure_workspace(str(ws))

    assert usage.files == 2              # two names
    assert usage.total_bytes == 100      # one inode's bytes


@pytest.mark.skipif(os.name != "posix", reason="FIFOs are POSIX-only")
def test_special_file_is_counted_but_never_opened(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    os.mkfifo(str(ws / "pipe"))
    usage = measure_workspace(str(ws))
    # Reaching this line at all proves the FIFO was not opened for reading
    # (opening it without a writer blocks forever).
    assert usage.special_files == 1
    assert usage.total_bytes == 0


def test_unreadable_subtree_is_reported_not_ignored(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws, "ok.txt", b"1")
    blocked = ws / "blocked"
    blocked.mkdir()

    real_scandir = os.scandir

    def fake_scandir(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(blocked)):
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    usage = measure_workspace(str(ws))
    assert usage.unreadable == 1
    assert usage.files == 1


# ---------------------------------------------------------------------------
# 2. Pre-write refusal — boundaries
# ---------------------------------------------------------------------------

def test_total_byte_boundary_is_exact(tmp_path):
    quota = WorkspaceQuota(max_total_bytes=10, max_file_bytes=None,
                           max_entries=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)

    enforcer.check_write(str(tmp_path / "exact.bin"), 10)      # exactly at limit
    (tmp_path / "exact.bin").write_bytes(b"x" * 10)

    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(str(tmp_path / "one-more.bin"), 1)
    assert exc.value.kind == "total"
    assert exc.value.used == 11
    assert exc.value.limit == 10


def test_per_file_boundary_is_exact(tmp_path):
    quota = WorkspaceQuota(max_file_bytes=5, max_total_bytes=None,
                           max_entries=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)

    enforcer.check_write(str(tmp_path / "ok.bin"), 5)
    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(str(tmp_path / "too-big.bin"), 6)
    assert exc.value.kind == "file"


def test_rewrite_credits_replaced_bytes(tmp_path):
    quota = WorkspaceQuota(max_total_bytes=10, max_file_bytes=None,
                           max_entries=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)
    _write(tmp_path, "doc.md", b"0123456789")   # exactly at the limit

    # Rewriting the same 10 bytes must be allowed: the old bytes are replaced.
    enforcer.check_write(str(tmp_path / "doc.md"), 10)

    # Growing it by one byte must not be.
    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(str(tmp_path / "doc.md"), 11)
    assert exc.value.kind == "total"


def test_entry_boundary_counts_directories_too(tmp_path):
    quota = WorkspaceQuota(max_entries=3, max_total_bytes=None,
                           max_file_bytes=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)

    # "a/b/c.txt" creates 2 directories + 1 file = 3 entries: allowed.
    enforcer.check_write(str(tmp_path / "a" / "b" / "c.txt"), 1)
    _write(tmp_path, "a/b/c.txt", b"x")

    # One more file would be the 4th entry: refused.
    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(str(tmp_path / "a" / "d.txt"), 1)
    assert exc.value.kind == "entries"


def test_depth_boundary(tmp_path):
    quota = WorkspaceQuota(max_depth=2, max_total_bytes=None,
                           max_file_bytes=None, max_entries=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)

    enforcer.check_write(os.path.join(str(tmp_path), "a", "b.txt"), 1)
    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(os.path.join(str(tmp_path), "a", "b", "c.txt"), 1)
    assert exc.value.kind == "depth"


def test_measurement_depth_violation_blocks_further_writes(tmp_path):
    quota = WorkspaceQuota(max_depth=1, max_total_bytes=None,
                           max_file_bytes=None, max_entries=None)
    _write(tmp_path, "a/b/c/deep.txt", b"x")
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)

    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_before_run()
    assert exc.value.kind == "depth"


# ---------------------------------------------------------------------------
# 3. Bypass attempts
# ---------------------------------------------------------------------------

def test_write_outside_scope_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    enforcer = WorkspaceEnforcer(str(ws), WorkspaceQuota())

    for dest in (str(tmp_path / "sibling.txt"),
                 os.path.join(str(ws), "..", "sibling.txt")):
        with pytest.raises(WorkspaceQuotaExceeded) as exc:
            enforcer.check_write(dest, 1)
        assert exc.value.kind == "outside"


def test_symlinked_parent_escaping_scope_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(outside, ws / "escape")

    enforcer = WorkspaceEnforcer(str(ws), WorkspaceQuota())
    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(os.path.join(str(ws), "escape", "pwned.txt"), 1)
    assert exc.value.kind == "outside"
    assert not (outside / "pwned.txt").exists()


def test_escape_symlink_already_present_fails_closed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret")
    _symlink_or_skip(outside, ws / "link")

    enforcer = WorkspaceEnforcer(str(ws), WorkspaceQuota())
    # Every write is refused, not just writes through the link: a scope whose
    # bytes cannot be trusted is not a scope to keep writing into.
    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(str(ws / "innocent.txt"), 1)
    assert exc.value.kind == "symlink_escape"


def test_allow_escaped_symlinks_is_an_explicit_opt_in(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    _symlink_or_skip(outside, ws / "link")

    enforcer = WorkspaceEnforcer(
        str(ws), WorkspaceQuota(allow_escaped_symlinks=True))
    enforcer.check_write(str(ws / "innocent.txt"), 1)   # allowed by the grant


def test_unmeasurable_workspace_refuses_writes(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws, "a.txt", b"1")

    import services.execution.workspace_quota as wq

    def unreadable(root):
        usage = wq.WorkspaceUsage(unreadable=1)
        return usage

    monkeypatch.setattr(wq, "measure_workspace", unreadable)
    enforcer = WorkspaceEnforcer(str(ws), WorkspaceQuota())
    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(str(ws / "b.txt"), 1)
    assert exc.value.kind == "indeterminate"


def test_concurrent_reservations_cannot_overshoot(tmp_path):
    quota = WorkspaceQuota(max_total_bytes=100, max_file_bytes=None,
                           max_entries=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)

    with enforcer.reserve(str(tmp_path / "first.bin"), 60):
        # 60 bytes are in flight; a second 60-byte write cannot be admitted.
        with pytest.raises(WorkspaceQuotaExceeded) as exc:
            enforcer.check_write(str(tmp_path / "second.bin"), 60)
        assert exc.value.kind == "total"
        assert exc.value.used == 120


def test_reservation_is_released_when_the_write_raises(tmp_path):
    quota = WorkspaceQuota(max_total_bytes=10, max_file_bytes=None,
                           max_entries=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)

    with pytest.raises(RuntimeError):
        with enforcer.reserve(str(tmp_path / "fail.bin"), 10):
            raise RuntimeError("write failed")

    # The 10 in-flight bytes were released, so the next 10-byte write fits.
    enforcer.check_write(str(tmp_path / "next.bin"), 10)


def test_many_small_files_hit_the_entry_quota(tmp_path):
    quota = WorkspaceQuota(max_entries=5, max_total_bytes=None,
                           max_file_bytes=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)
    written = 0
    for i in range(10):
        dest = str(tmp_path / f"f{i}.txt")
        try:
            enforcer.check_write(dest, 1)
        except WorkspaceQuotaExceeded as exc:
            assert exc.kind == "entries"
            break
        (tmp_path / f"f{i}.txt").write_bytes(b"x")
        written += 1
    assert written == 5


# ---------------------------------------------------------------------------
# 4. Cleanup / reclaim
# ---------------------------------------------------------------------------

def test_deleting_files_reclaims_the_quota(tmp_path):
    quota = WorkspaceQuota(max_total_bytes=10, max_file_bytes=None,
                           max_entries=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)
    path = _write(tmp_path, "a.bin", b"x" * 10)

    with pytest.raises(WorkspaceQuotaExceeded):
        enforcer.check_write(str(tmp_path / "b.bin"), 1)

    os.unlink(path)
    enforcer.check_write(str(tmp_path / "b.bin"), 10)   # reclaimed


def test_refused_write_leaves_no_usage_behind(tmp_path):
    quota = WorkspaceQuota(max_total_bytes=5, max_file_bytes=None,
                           max_entries=None, max_depth=None)
    enforcer = WorkspaceEnforcer(str(tmp_path), quota)
    with pytest.raises(WorkspaceQuotaExceeded):
        with enforcer.reserve(str(tmp_path / "nope.bin"), 6):
            pass
    assert measure_workspace(str(tmp_path)).total_bytes == 0
    assert enforcer.usage.total_bytes == 0


# ---------------------------------------------------------------------------
# 5. Adapter integration — refused BEFORE bytes land
# ---------------------------------------------------------------------------

async def test_fs_write_refuses_oversized_payload_before_writing(tmp_path):
    adapter = FileWriteAdapter(
        str(tmp_path), quota=WorkspaceQuota(max_file_bytes=8,
                                            max_total_bytes=None,
                                            max_entries=None, max_depth=None))
    result = await adapter.run({"path": "big.txt", "content": "x" * 9},
                               _ctx())

    assert result.success is False
    assert "Workspace write refused" in (result.error or "")
    assert "file" in (result.error or "")
    assert not (tmp_path / "t1" / "big.txt").exists()
    assert measure_workspace(str(tmp_path / "t1")).total_bytes == 0


async def test_fs_write_allows_within_quota_and_creates_directories(tmp_path):
    adapter = FileWriteAdapter(str(tmp_path))
    result = await adapter.run(
        {"path": "sub/dir/note.txt", "content": "hello",
         "create_directories": True}, _ctx())

    assert result.success is True, result.error
    written = tmp_path / "t1" / "sub" / "dir" / "note.txt"
    assert written.read_text() == "hello"


async def test_fs_write_quota_is_per_task_scope(tmp_path):
    adapter = FileWriteAdapter(
        str(tmp_path), quota=WorkspaceQuota(max_total_bytes=10,
                                            max_file_bytes=None,
                                            max_entries=None, max_depth=None))
    first = await adapter.run({"path": "a.txt", "content": "0123456789"},
                              _ctx(task="task-a"))
    assert first.success is True, first.error

    refused = await adapter.run({"path": "a.txt", "content": "0123456789"},
                                _ctx(task="task-b"))
    # A different task scope is a different budget: it still has room.
    assert refused.success is True, refused.error

    full = await adapter.run({"path": "b.txt", "content": "x"},
                             _ctx(task="task-a"))
    assert full.success is False
    assert "total" in (full.error or "")


async def test_fs_write_complains_about_an_escaped_link_in_the_scope(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    adapter = FileWriteAdapter(str(tmp_path))
    scope = tmp_path / "t1"
    scope.mkdir()
    _symlink_or_skip(outside, scope / "link")

    result = await adapter.run({"path": "innocent.txt", "content": "x"},
                               _ctx())
    assert result.success is False
    assert "symlink_escape" in (result.error or "")
    assert not (scope / "innocent.txt").exists()


# ---------------------------------------------------------------------------
# 6. Sandbox integration — pre-run gate and post-run honesty
# ---------------------------------------------------------------------------

def _spec(cwd, argv) -> SandboxSpec:
    return SandboxSpec(argv=argv, cwd=str(cwd), env={}, timeout_s=20,
                       workspace_writable=str(cwd))


async def test_sandbox_refuses_to_run_when_workspace_is_over_quota(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws, "existing.bin", b"x" * 200)

    sandbox = ProcessSandbox(
        workspace_quota=WorkspaceQuota(max_total_bytes=100, max_file_bytes=None,
                                       max_entries=None, max_depth=None))
    canary = ws / "should-not-exist.txt"
    result = await sandbox.run(_spec(
        ws, [sys.executable, "-c",
             "open('should-not-exist.txt','w').write('ran')"]))

    assert result.success is False
    assert "workspace quota" in (result.error or "")
    assert result.killed_reason == "supervisor"
    assert "disk" in result.limits_hit and "disk:total" in result.limits_hit
    assert result.enforced == ["workspace_quota"]
    assert not canary.exists()          # nothing was spawned


async def test_sandbox_reports_a_child_that_overfills_the_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sandbox = ProcessSandbox(
        workspace_quota=WorkspaceQuota(max_total_bytes=500,
                                       max_file_bytes=None,
                                       max_entries=None, max_depth=None))

    result = await sandbox.run(_spec(
        ws, [sys.executable, "-c",
             "open('flood.bin','wb').write(b'x'*2000)"]))

    # The child exited 0; the run is reported honestly rather than being
    # rewritten into a fake quota kill.
    assert result.exit_code == 0
    assert result.killed_reason is None
    assert "disk" in result.limits_hit and "disk:total" in result.limits_hit
    assert "[sandbox: workspace quota refused (total)" in result.stderr
    assert (ws / "flood.bin").stat().st_size == 2000


async def test_clean_run_gains_no_disk_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sandbox = ProcessSandbox(workspace_quota=WorkspaceQuota())
    result = await sandbox.run(_spec(
        ws, [sys.executable, "-c", "open('ok.txt','w').write('hi')"]))
    assert result.success is True, result.error
    assert not any(str(x).startswith("disk") for x in result.limits_hit)
    assert "workspace_quota" in result.enforced


# ---------------------------------------------------------------------------
# 7. Cumulative disk budget (churn) vs standing workspace size
# ---------------------------------------------------------------------------

def test_budget_disk_dimension_exhausts_and_is_reported():
    budget = ExecutionBudget(disk_bytes=100)
    budget.record_usage(disk_bytes=60)
    budget.check_reserve()                       # still under
    budget.record_usage(disk_bytes=40)
    with pytest.raises(Exception) as exc:
        budget.check_reserve()
    assert getattr(exc.value, "kind", None) == "disk"
    status = budget.status()
    assert status["disk_bytes_limit"] == 100
    assert status["disk_bytes_used"] == 100
    assert "disk" in status["exhausted"]


def test_enforcer_refuses_writes_when_the_disk_budget_is_spent(tmp_path):
    budget = ExecutionBudget(disk_bytes=10)
    budget.record_usage(disk_bytes=10)
    enforcer = WorkspaceEnforcer(str(tmp_path), WorkspaceQuota(), budget=budget)

    with pytest.raises(WorkspaceQuotaExceeded) as exc:
        enforcer.check_write(str(tmp_path / "a.txt"), 1)
    assert exc.value.kind == "budget"


def test_record_after_run_consumes_growth_into_the_budget(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    budget = ExecutionBudget(disk_bytes=10_000)
    enforcer = WorkspaceEnforcer(str(ws), WorkspaceQuota(), budget=budget)

    enforcer.check_before_run()                  # baseline
    _write(ws, "artifact.bin", b"x" * 300)
    violation = enforcer.record_after_run()

    assert violation is None
    assert budget.disk_used == 300


def test_quota_cache_returns_one_enforcer_per_scope(tmp_path):
    cache = WorkspaceQuotaCache(WorkspaceQuota())
    a1 = cache.for_scope(str(tmp_path / "a"))
    a2 = cache.for_scope(str(tmp_path / "a"))
    b1 = cache.for_scope(str(tmp_path / "b"))
    assert a1 is a2
    assert a1 is not b1
    (tmp_path / "a").mkdir()
    _write(tmp_path / "a", "f.txt", b"123")
    assert cache.snapshot()[str(tmp_path / "a")]["total_bytes"] == 3
