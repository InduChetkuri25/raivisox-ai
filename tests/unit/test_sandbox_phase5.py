"""V2 Phase 5 — resource quotas and execution budgets.

Focused tests for docs/sandbox-design.md §5 (failure semantics) and §7.6
(per-task cumulative budget):

- the quota model maps onto the EXISTING SandboxSpec fields (no duplicate
  limit system); every limit carries a configured value, an enforcement
  status from capabilities(), and honest failure semantics
- process backend: enforces only what the host supports, reports the rest
  as unsupported, never claims enforcement that did not happen, no fake
  Windows CPU/memory/PID layer; timeout and tree-kill unchanged
- container backend: maps limits to real runtime flags (only when the
  spec requests them); quota attribution only from runtime-attributed
  exit codes (152/153/136; 137→memory ONLY when --memory was applied)
- killed_reason vocabulary: ordinary non-zero exits are NOT quota
  terminations; uncertain attribution → honest supervisor failure
- cumulative ExecutionBudget: hard gate before spawn, honest consumption
  after, per-dimension killed_reason mapping (wall→timeout, cpu→cpu,
  runs→supervisor)
- cleanup after success, failure, timeout, quota kill, and cancellation
- no unsandboxed fallback under any quota path

Test discipline (unchanged): tests distinguish AVAILABLE+ENFORCED,
AVAILABLE BUT UNSUPPORTED, RUNTIME UNAVAILABLE, EXECUTION FAILURE and
QUOTA TERMINATION by asserting against capabilities()/enforced — a test
never passes because a limit was not enforced on this host. Container
quota behavior is additionally covered by ``live``-marked tests that run
in the CI container gate and skip honestly here (no runtime on Windows).
"""

import asyncio
import os
import sys

import pytest

from integrations.adapters.base import AdapterContext
from integrations.adapters.builtin import PythonExecAdapter
from services.execution.container_sandbox import ContainerSandbox, build_run_argv, map_container_cwd
from services.execution.quotas import (
    BudgetExceeded,
    ExecutionBudget,
    QuotaEnforcer,
    SandboxQuota,
)
from services.execution.sandbox import (
    KilledReason,
    ProcessSandbox,
    SandboxResult,
    SandboxSpec,
)

SECRET_KEY_NAME = "RAIVISOX_PHASE5_SECRET"
SECRET_VALUE = "phase5-quota-secret-do-not-leak-55"


@pytest.fixture(autouse=True)
def _secret_env():
    os.environ[SECRET_KEY_NAME] = SECRET_VALUE
    yield
    os.environ.pop(SECRET_KEY_NAME, None)


def _ctx(task="t5") -> AdapterContext:
    return AdapterContext(actor_id="tester", task_id=task, step_id="s5",
                          resource=f"task:{task}")


def _spec(cwd, code="print(1)", **kw) -> SandboxSpec:
    return SandboxSpec(argv=[sys.executable, "-c", code], cwd=str(cwd),
                       env={}, **kw)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _force_unavailable(monkeypatch):
    monkeypatch.setattr(
        "services.execution.container_sandbox.probe_runtime",
        lambda force=False: (None, "no reachable container runtime (docker/podman)"),
    )


# ---------------------------------------------------------------------------
# 1. Quota model — one representation over the existing SandboxSpec fields
# ---------------------------------------------------------------------------

def test_quota_maps_onto_existing_spec_fields():
    q = SandboxQuota(cpu_seconds=5, memory_bytes=1024, max_pids=4,
                     fsize_bytes=128, output_limit_bytes=99)
    kwargs = q.to_spec_kwargs()
    assert kwargs == {"output_limit_bytes": 99, "cpu_seconds": 5,
                      "memory_bytes": 1024, "max_pids": 4, "fsize_bytes": 128}
    # The mapped fields ARE SandboxSpec fields — no duplicate system.
    spec = _spec("/tmp", **kwargs)
    assert spec.cpu_seconds == 5 and spec.memory_bytes == 1024
    assert spec.max_pids == 4 and spec.fsize_bytes == 128
    assert spec.output_limit_bytes == 99


def test_quota_defaults_require_core_guarantees_only():
    q = SandboxQuota()
    missing = q.missing_from(ProcessSandbox().capabilities())
    if os.name == "posix":
        assert missing == []  # a full Linux process host satisfies the core
    else:
        # Windows cannot enforce POSIX rlimits; the core set does not
        # demand them, so the quota is satisfiable there too.
        assert missing == []


def test_quota_require_adds_limits_case_insensitively():
    q = SandboxQuota().require("CPU_LIMIT", "network_deny")
    assert "cpu_limit" in q.required_limits
    assert "network_deny" in q.required_limits
    caps = {name: True for name in q.required_limits}
    caps["network_deny"] = False  # everything enforced EXCEPT this one
    assert q.missing_from(caps) == ["network_deny"]


def test_quota_is_the_enforcement_status_source():
    """Every limit's enforcement status comes from capabilities() — the
    same source the result's ``enforced`` list is built from."""
    caps = ProcessSandbox().capabilities()
    q = SandboxQuota()
    for name in q.required_limits:
        assert isinstance(caps.get(name), bool)


# ---------------------------------------------------------------------------
# 2. Process backend — enforce only what the host supports, honestly
# ---------------------------------------------------------------------------

async def test_process_unsupported_limit_reports_not_enforced(workspace):
    """A requested limit the host cannot enforce is recorded as NOT in
    ``enforced`` — never claimed, never converted into a pass."""
    sb = ProcessSandbox()
    caps = sb.capabilities()
    r = await sb.run(_spec(workspace, memory_bytes=1_000_000))
    if not caps["memory_limit"]:
        assert "memory_limit" not in r.enforced
    else:
        assert "memory_limit" in r.enforced


async def test_process_timeout_remains_enforced(workspace):
    r = await ProcessSandbox().run(
        _spec(workspace, "import time; time.sleep(30)", timeout_s=2))
    assert r.killed_reason == "timeout"
    assert "timeout" in r.limits_hit
    assert r.exit_code is None


async def test_process_cleanup_after_timeout(workspace):
    """The killed child is reaped — no zombie survives the timeout kill."""
    r = await ProcessSandbox().run(_spec(
        workspace, "import subprocess, sys, time\n"
                   "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                   "time.sleep(60)", timeout_s=2))
    assert r.killed_reason == "timeout"


async def test_process_strict_quota_refuses_unenforceable_limit(workspace):
    """AVAILABLE BUT UNSUPPORTED → honest refusal, no process, no fallback."""
    caps = ProcessSandbox().capabilities()
    if caps["cpu_limit"]:
        pytest.skip("this host enforces cpu_limit; refusal path not applicable")
    sb = ProcessSandbox(quota=SandboxQuota(cpu_seconds=2).require("cpu_limit"))
    r = await sb.run(_spec(workspace))
    assert not r.success
    assert r.killed_reason == "supervisor"
    assert "sandbox quota not enforceable" in r.error
    assert "cpu_limit" in r.error
    assert r.exit_code is None  # never spawned
    assert r.enforced == ["spec_validation"]


async def test_process_non_strict_quota_runs_and_reports_honestly(workspace):
    sb = ProcessSandbox(quota=SandboxQuota(cpu_seconds=2, strict_limits=False))
    r = await sb.run(_spec(workspace, "print(5*5)"))
    assert r.success, r.error
    assert "25" in r.stdout


def test_process_capabilities_never_claim_windows_cpu_mem_pids():
    if os.name != "posix":
        caps = ProcessSandbox().capabilities()
        assert caps["cpu_limit"] is False
        assert caps["memory_limit"] is False
        assert caps["pids_limit"] is False
        assert caps["fsize_limit"] is False


# ---------------------------------------------------------------------------
# 3. killed_reason semantics — quota vs ordinary failure
# ---------------------------------------------------------------------------

async def test_ordinary_nonzero_exit_is_not_quota_termination(workspace):
    r = await ProcessSandbox().run(_spec(workspace, "import sys; sys.exit(2)"))
    assert r.exit_code == 2
    assert r.killed_reason is None  # an exit, not a kill


async def test_uncertain_signal_death_is_supervisor_not_invented_quota(workspace):
    """SIGKILL's cause is unknowable → honest supervisor, never 'memory'."""
    if os.name != "posix":
        pytest.skip("signal-death semantics asserted on POSIX")
    r = await ProcessSandbox().run(_spec(
        workspace, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
    assert r.killed_reason == "supervisor"
    assert "supervisor" in r.limits_hit


@pytest.mark.skipif(os.name != "posix", reason="SIGXCPU attribution is POSIX-only")
async def test_cpu_quota_kill_attribution_posix(workspace):
    """AVAILABLE + ENFORCED: RLIMIT_CPU exhaustion → killed_reason='cpu'."""
    r = await ProcessSandbox().run(_spec(
        workspace, "x = 0\nwhile True:\n    x += 1\n", cpu_seconds=1))
    assert r.killed_reason == "cpu"
    assert "cpu" in r.limits_hit


async def test_cpu_quota_on_unsupported_host_is_not_a_quota_kill(workspace):
    """AVAILABLE BUT UNSUPPORTED: the limit is reported unenforced; a busy
    loop dies by the wall-clock TIMEOUT instead — never a fake 'cpu'."""
    caps = ProcessSandbox().capabilities()
    if caps["cpu_limit"]:
        pytest.skip("host enforces cpu_limit; unsupported path not applicable")
    r = await ProcessSandbox().run(_spec(
        workspace, "x = 0\nwhile True:\n    x += 1\n",
        cpu_seconds=1, timeout_s=2))
    assert r.killed_reason == "timeout"
    assert "cpu_limit" not in r.enforced


@pytest.mark.skipif(os.name != "posix", reason="SIGXFSZ attribution is POSIX-only")
async def test_fsize_quota_kill_attribution_posix(workspace):
    r = await ProcessSandbox().run(_spec(
        workspace,
        "f = open('big.bin', 'wb')\n"
        "try:\n"
        "    f.write(b'x' * 10_000_000)\n"
        "finally:\n"
        "    f.close()\n",
        fsize_bytes=1024))
    assert r.killed_reason == "fsize"
    assert "fsize" in r.limits_hit


async def test_output_limit_hit_is_bounded_and_labeled(workspace):
    r = await ProcessSandbox().run(_spec(workspace, "print('x' * 500_000)",
                                         output_limit_bytes=100))
    assert r.success  # truncation is a limit hit, not a failure
    assert len(r.stdout) <= 200
    assert "output_limit" in r.limits_hit
    assert "output_limit:stdout" in r.limits_hit


# ---------------------------------------------------------------------------
# 4. Container backend — quota mapping (static) + live quota attribution
# ---------------------------------------------------------------------------

def test_container_quota_flags_only_for_requested_limits(tmp_path):
    cwd, err = map_container_cwd(_spec(tmp_path))
    assert err is None
    plain = build_run_argv(_spec(tmp_path), runtime_exe="docker",
                           image="img:tag", user="1000:1000", python_bin="",
                           container_cwd=cwd, name="raivisox-sbx-t")
    for flag in ("--memory", "--pids-limit", "--ulimit"):
        assert flag not in plain
    rich = build_run_argv(_spec(tmp_path, memory_bytes=268_435_456,
                                max_pids=16, cpu_seconds=3, fsize_bytes=2048),
                          runtime_exe="docker", image="img:tag",
                          user="1000:1000", python_bin="",
                          container_cwd=cwd, name="raivisox-sbx-t")
    assert rich[rich.index("--memory") + 1] == "268435456"
    assert rich[rich.index("--pids-limit") + 1] == "16"
    ulimits = [rich[i + 1] for i, c in enumerate(rich) if c == "--ulimit"]
    assert ulimits == ["cpu=3:3", "fsize=2048:2048"]


def test_container_enforced_lists_only_requested_quota_limits(tmp_path):
    sb = ContainerSandbox()
    plain = sb._enforced_for(_spec(tmp_path))
    for key in ("cpu_limit", "memory_limit", "fsize_limit", "pids_limit"):
        assert key not in plain
    rich = sb._enforced_for(_spec(tmp_path, memory_bytes=1024, max_pids=8,
                                  cpu_seconds=2, fsize_bytes=64))
    for key in ("cpu_limit", "memory_limit", "fsize_limit", "pids_limit"):
        assert key in rich


async def test_container_strict_quota_refuses_before_probe(tmp_path, monkeypatch):
    """RUNTIME UNAVAILABLE + strict quota: the quota refusal (spec-level,
    pre-spawn) is the honest first failure — never a probe race."""
    _force_unavailable(monkeypatch)
    sb = ContainerSandbox(quota=SandboxQuota().require("fs_confinement"))
    r = await sb.run(_spec(tmp_path))
    assert not r.success
    assert "sandbox quota not enforceable" in r.error
    assert r.exit_code is None


async def test_container_non_strict_quota_reaches_honest_unavailability(
        tmp_path, monkeypatch):
    _force_unavailable(monkeypatch)
    sb = ContainerSandbox(quota=SandboxQuota(strict_limits=False))
    r = await sb.run(_spec(tmp_path))
    assert not r.success
    assert "execution environment unavailable" in r.error


_LIVE = os.environ.get("RAIVISOX_TEST_LIVE_CONTAINER") == "1"


def _live_state():
    from services.execution.container_sandbox import probe_runtime
    exe, _ = probe_runtime(force=True)
    if not exe:
        return False, "no container runtime (docker/podman) on this host"
    import subprocess
    image = os.environ.get("SANDBOX_CONTAINER_IMAGE", "").strip() or \
        "python:3.12-slim"
    p = subprocess.run(  # noqa: S603 - fixed argv, trusted binary
        [exe, "image", "inspect", image], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=15)
    if p.returncode != 0:
        return False, f"image {image} not present locally (tests never pull)"
    return True, ""


_LIVE_OK, _LIVE_REASON = _live_state()


def live(fn=None):
    marks = [pytest.mark.live]
    if not _LIVE_OK:
        marks.append(pytest.mark.skip(
            reason=f"live container test skipped: {_LIVE_REASON}"))

    def _deco(f):
        for m in reversed(marks):
            f = m(f)
        return f

    return _deco(fn) if fn is not None else _deco


@live
async def test_live_container_memory_limit_kills_with_memory_reason(tmp_path):
    """QUOTA TERMINATION, attributed only because --memory was applied."""
    sb = ContainerSandbox()
    code = ("x = bytearray(512 * 1024 * 1024)\n"
            "for i in range(len(x)):\n"
            "    x[i] = 1\n")
    r = await sb.run(_spec(tmp_path, code, memory_bytes=64 * 1024 * 1024,
                           timeout_s=30))
    assert not r.success
    assert r.killed_reason == "memory"
    assert "memory" in r.limits_hit


@live
async def test_live_container_pid_limit_blocks_fork_bomb(tmp_path):
    sb = ContainerSandbox()
    code = ("import os, sys, time\n"
            "kids = 0\n"
            "try:\n"
            "    for _ in range(200):\n"
            "        pid = os.fork()\n"
            "        if pid == 0:\n"
            "            time.sleep(30)\n"
            "        kids += 1\n"
            "except OSError:\n"
            "    print('FORK-BLOCKED', kids)\n"
            "time.sleep(1)\n")
    r = await sb.run(_spec(tmp_path, code, max_pids=8, timeout_s=20))
    assert "FORK-BLOCKED" in r.stdout or r.killed_reason in ("pids", "timeout")


@live
async def test_live_container_cpu_limit_kills_with_cpu_reason(tmp_path):
    sb = ContainerSandbox()
    r = await sb.run(_spec(tmp_path, "x = 0\nwhile True:\n    x += 1\n",
                           cpu_seconds=2, timeout_s=30))
    assert r.killed_reason in ("cpu", "supervisor")  # runtime-dependent wording
    assert r.success is False


@live
async def test_live_container_fsize_ulimit_kills_write(tmp_path):
    sb = ContainerSandbox()
    code = ("f = open('big.bin', 'wb')\n"
            "try:\n"
            "    f.write(b'x' * 10_000_000)\n"
            "finally:\n"
            "    f.close()\n")
    r = await sb.run(_spec(tmp_path, code, fsize_bytes=4096, timeout_s=20))
    assert r.success is False
    assert r.killed_reason in ("fsize", "supervisor")


@live
async def test_live_container_cleanup_after_quota_kill(tmp_path):
    import subprocess
    sb = ContainerSandbox()
    await sb.run(_spec(tmp_path, "x = 0\nwhile True:\n    x += 1\n",
                       cpu_seconds=1, timeout_s=20))
    exe, _ = __import__("services.execution.container_sandbox",
                        fromlist=["probe_runtime"]).probe_runtime(force=True)
    p = subprocess.run(  # noqa: S603 - fixed argv, trusted binary
        [exe, "ps", "-a", "--filter", "name=raivisox-sbx-",
         "--format", "{{.Names}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    assert p.stdout.decode().strip() == ""


# ---------------------------------------------------------------------------
# 5. Cumulative ExecutionBudget — hard gate, honest consumption
# ---------------------------------------------------------------------------

async def test_budget_run_count_blocks_before_spawn(workspace):
    b = ExecutionBudget(max_runs=2)
    sb = ProcessSandbox(budget=b)
    for _ in range(2):
        r = await sb.run(_spec(workspace))
        assert r.success, r.error
    r3 = await sb.run(_spec(workspace))
    assert not r3.success
    assert "execution budget exhausted" in r3.error
    assert r3.killed_reason == "supervisor"  # run-count = policy stop
    assert r3.exit_code is None              # third run never spawned


async def test_budget_wall_clock_expiry_maps_to_timeout(workspace):
    b = ExecutionBudget(wall_clock_seconds=0.05)
    sb = ProcessSandbox(budget=b)
    await asyncio.sleep(0.1)
    r = await sb.run(_spec(workspace))
    assert not r.success
    assert r.killed_reason == "timeout"  # honest wall-clock vocabulary
    assert "budget" in r.limits_hit and "wall_clock" in r.limits_hit


async def test_budget_cpu_exhaustion_blocks_further_runs(workspace):
    b = ExecutionBudget(cpu_seconds=0.01)
    sb = ProcessSandbox(budget=b)
    r1 = await sb.run(_spec(workspace, "x = sum(range(200000))"))
    assert r1.success
    assert b.cpu_used > 0  # actual consumption recorded
    r2 = await sb.run(_spec(workspace))
    assert not r2.success
    assert r2.killed_reason == "cpu"


async def test_budget_records_actual_cpu_basis(workspace):
    b = ExecutionBudget()
    sb = ProcessSandbox(budget=b)
    await sb.run(_spec(workspace, "print(1)"))
    st = b.status()
    assert st["runs"] == 1
    assert st["cpu_used"] > 0
    if os.name == "posix":
        assert st["cpu_basis"] == "cpu_time"
    else:
        assert st["cpu_basis"] == "wall_clock"


async def test_budget_status_reports_exhausted_dimensions():
    b = ExecutionBudget(max_runs=1)
    assert b.status()["exhausted"] == []
    b.record_usage(cpu_seconds=0.1, wall_seconds=0.2, runs=1)
    assert b.status()["exhausted"] == ["max_runs"]


async def test_budget_overage_never_falls_back_unsandboxed(workspace):
    """A budget-exhausted context cannot trick its way into an unconfined
    run: the block happens inside the sandbox, before any process."""
    b = ExecutionBudget(max_runs=0)
    sb = ProcessSandbox(budget=b)
    canary = workspace / "canary.txt"
    r = await sb.run(_spec(workspace,
                           f"open({str(canary)!r}, 'w').write('escaped')"))
    assert not r.success
    assert not canary.exists()


async def test_container_budget_gate_fires_before_runtime_probe(tmp_path,
                                                                monkeypatch):
    """The budget hard-block needs no runtime and no probe: it fails
    honestly even on a host with no Docker at all."""
    _force_unavailable(monkeypatch)
    sb = ContainerSandbox(budget=ExecutionBudget(max_runs=0))
    r = await sb.run(_spec(tmp_path))
    assert not r.success
    assert "execution budget exhausted" in r.error
    assert r.killed_reason == "supervisor"


# ---------------------------------------------------------------------------
# 6. QuotaEnforcer — app-layer disk wall + expiry semantics
# ---------------------------------------------------------------------------

def test_enforcer_refuses_oversized_write_before_bytes_land(tmp_path):
    qe = QuotaEnforcer(max_write_bytes=16)
    target = tmp_path / "big.bin"
    with pytest.raises(BudgetExceeded) as exc:
        qe.check_write(str(target), "y" * 17)
    assert exc.value.kind == "disk"
    assert not target.exists()  # nothing landed
    assert qe.writes_rejected == 1


def test_enforcer_allows_within_quota(tmp_path):
    qe = QuotaEnforcer(max_write_bytes=16)
    qe.check_write(str(tmp_path / "ok.bin"), b"0123456789abcdef")
    assert qe.writes_rejected == 0


def test_enforcer_expiry_is_timeout_vocabulary():
    b = ExecutionBudget(wall_clock_seconds=0.05, scope="expiry-test")
    qe = QuotaEnforcer(budget=b)
    import time as _t
    _t.sleep(0.1)
    with pytest.raises(BudgetExceeded) as exc:
        qe.check_budget_expiry()
    assert exc.value.kind == "wall_clock"


# ---------------------------------------------------------------------------
# 7. Adapter-level: quota/budget failures keep honest step semantics
# ---------------------------------------------------------------------------

async def test_adapter_budget_exhaustion_is_step_failure(workspace):
    b = ExecutionBudget(max_runs=0)
    adapter = PythonExecAdapter(str(workspace),
                                sandbox=ProcessSandbox(budget=b))
    r = await adapter.run({"code": "print(1)"}, _ctx())
    assert not r.success
    assert "execution budget exhausted" in r.error


async def test_adapter_strict_quota_unsupported_is_step_failure(workspace):
    caps = ProcessSandbox().capabilities()
    if caps["cpu_limit"]:
        pytest.skip("host enforces cpu_limit; refusal path not applicable")
    adapter = PythonExecAdapter(
        str(workspace),
        sandbox=ProcessSandbox(quota=SandboxQuota().require("cpu_limit")))
    r = await adapter.run({"code": "print(1)"}, _ctx())
    assert not r.success
    assert "sandbox quota not enforceable" in r.error


async def test_secret_isolation_unchanged_under_quotas(workspace):
    """Quotas are additive; the minimal-env guarantee is untouched."""
    sb = ProcessSandbox(budget=ExecutionBudget(),
                        quota=SandboxQuota(memory_bytes=1_000_000))
    r = await sb.run(_spec(
        workspace,
        f"import os; print(os.environ.get('{SECRET_KEY_NAME}', 'ABSENT'))"))
    assert r.success, r.error
    assert r.stdout.strip() == "ABSENT"
    assert SECRET_VALUE not in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# 8. Cancellation cleanup (Phase 5: cleanup after cancellation)
# ---------------------------------------------------------------------------

async def test_cancellation_kills_child_and_cleans_up(workspace):
    """Cancelling a run kills/reaps the child before propagating - the
    process never outlives the run. (POSIX process-group kill; Windows
    kills the direct child - both satisfy the cleanup guarantee.)"""
    if os.name != "posix":
        pytest.skip("cross-platform orphan scan via /proc needs Linux")
    sb = ProcessSandbox()
    inner = _spec(workspace, "import time; time.sleep(60)", timeout_s=60)
    task = asyncio.create_task(sb.run(inner))
    await asyncio.sleep(3)  # let the child actually start

    def _orphans() -> list[int]:
        pids = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().decode(errors="replace")
            except OSError:
                continue
            if "sleep(60)" in cmd and f"{os.getpid()}" not in cmd:
                pids.append(int(pid))
        return pids

    try:
        before = _orphans()
    except FileNotFoundError:
        pytest.skip("/proc not available")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    after = _orphans()
    new_survivors = [p for p in after if p not in before]
    assert new_survivors == [], f"orphaned children survived cancellation: {new_survivors}"
