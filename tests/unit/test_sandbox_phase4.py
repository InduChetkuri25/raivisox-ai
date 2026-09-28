"""V2 Phase 4 — container-backed ExecutionSandbox backend tests.

Focused tests for docs/sandbox-design.md §7 phase 5 (container backend):

- backend selection: SANDBOX_BACKEND=process|container, default preserved,
  invalid config raises (never a silent fallback to another backend)
- fail-closed unavailability: honest unavailable result, no process-backend
  substitution, proof that the command did NOT execute on the host (canary)
- spec validation parity with the process backend (same boundary, same words)
- run-argv security properties (pure, no runtime needed): single workspace
  mount, no runtime socket, no --privileged, never --network host,
  cap-drop/no-new-privileges/read-only/non-root, explicit environment only,
  argv never joined (metacharacters stay inert data)
- bounded output drain (never unbounded memory)
- live container tests: executed ONLY where a runtime AND the image exist;
  skipped elsewhere with an explicit reason — a live container result is
  never faked. On a host without docker/podman the unavailable path is what
  is actually verified here.
"""

import asyncio
import os
import subprocess
import sys

import pytest

from integrations.adapters.base import AdapterContext
from integrations.adapters.builtin import PythonExecAdapter, ShellExecAdapter
from services.execution.container_sandbox import (
    CONTAINER_WORKSPACE,
    DEFAULT_IMAGE,
    ContainerSandbox,
    build_container_env,
    build_run_argv,
    decode_bounded,
    map_container_cwd,
    probe_runtime,
    read_bounded,
)
from services.execution.sandbox import (
    ProcessSandbox,
    SandboxSpec,
    build_default_sandbox,
)
from services.execution.terminal_sandbox import TerminalSandbox

SECRET_KEY_NAME = "RAIVISOX_PHASE4_SECRET"
SECRET_VALUE = "s3cret-phase4-never-in-container-99"


@pytest.fixture(autouse=True)
def _secret_env():
    os.environ[SECRET_KEY_NAME] = SECRET_VALUE
    yield
    os.environ.pop(SECRET_KEY_NAME, None)


def _ctx(task="t1") -> AdapterContext:
    return AdapterContext(actor_id="tester", task_id=task, step_id="s1",
                          resource=f"task:{task}")


def _spec(cwd, argv=None, **kw) -> SandboxSpec:
    return SandboxSpec(argv=argv or [sys.executable, "-c", "print(1)"],
                       cwd=str(cwd), env={}, **kw)


def _force_unavailable(monkeypatch):
    """Deterministically simulate a host with no reachable container runtime."""
    monkeypatch.setattr(
        "services.execution.container_sandbox.probe_runtime",
        lambda force=False: (None, "no reachable container runtime (docker/podman)"),
    )


# ---------------------------------------------------------------------------
# 1. Backend selection
# ---------------------------------------------------------------------------

def test_default_backend_is_process(monkeypatch):
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    assert isinstance(build_default_sandbox(), ProcessSandbox)


def test_explicit_process_backend(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "process")
    assert isinstance(build_default_sandbox(), ProcessSandbox)


def test_container_backend_selected_without_silent_swap(monkeypatch, tmp_path):
    """Config selects the container backend — even (especially) when no
    runtime exists: selection must never fall back to ProcessSandbox."""
    monkeypatch.setenv("SANDBOX_BACKEND", "container")
    sb = build_default_sandbox()
    assert isinstance(sb, ContainerSandbox)


def test_selection_tolerates_case_and_whitespace(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "  Container ")
    assert isinstance(build_default_sandbox(), ContainerSandbox)


def test_invalid_backend_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "containerd")
    with pytest.raises(ValueError) as exc:
        build_default_sandbox()
    assert "SANDBOX_BACKEND" in str(exc.value)
    assert "containerd" in str(exc.value)


def test_empty_backend_is_configuration_error(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "")
    with pytest.raises(ValueError):
        build_default_sandbox()


# ---------------------------------------------------------------------------
# 2. Unavailable runtime — honest failure, no fallback, no host execution
# ---------------------------------------------------------------------------

async def test_unavailable_run_fails_honestly(tmp_path, monkeypatch):
    _force_unavailable(monkeypatch)
    sb = ContainerSandbox()
    r = await sb.run(_spec(tmp_path, argv=["/bin/sh", "-c", "echo hi"]))
    assert not r.success
    assert r.error is not None
    assert "execution environment unavailable" in r.error
    assert "container runtime" in r.error
    assert r.killed_reason == "supervisor"
    assert r.exit_code is None  # never spawned anywhere
    assert r.enforced == []      # nothing enforced → nothing claimed


async def test_unavailable_capabilities_all_false(tmp_path, monkeypatch):
    _force_unavailable(monkeypatch)
    caps = ContainerSandbox().capabilities()
    assert caps["runtime_available"] is False
    # No runtime → every guarantee is reported as NOT enforced. A test
    # never passes because a limit "wasn't enforced" — it must be False.
    assert all(v is False for v in caps.values()), caps


async def test_unavailable_python_exec_canary_never_runs_on_host(
        tmp_path, monkeypatch):
    """The command must NOT execute on the host when the container
    backend is unavailable — no ProcessSandbox substitution exists."""
    _force_unavailable(monkeypatch)
    canary = tmp_path / "canary.txt"
    adapter = PythonExecAdapter(str(tmp_path), sandbox=ContainerSandbox())
    r = await adapter.run(
        {"code": f"open({str(canary)!r}, 'w').write('escaped')"}, _ctx())
    assert not r.success
    assert r.error is not None and "unavailable" in r.error
    assert not canary.exists()  # proof: nothing executed anywhere


async def test_unavailable_shell_adapter_fails_honestly(tmp_path, monkeypatch):
    _force_unavailable(monkeypatch)
    adapter = ShellExecAdapter(str(tmp_path), exec_sandbox=ContainerSandbox())
    r = await adapter.run({"command": "echo hi"}, _ctx())
    assert not r.success
    assert r.error is not None and "unavailable" in r.error


async def test_unavailable_terminal_tool_fails_honestly(tmp_path, monkeypatch):
    _force_unavailable(monkeypatch)
    ts = TerminalSandbox(working_dir=str(tmp_path), exec_sandbox=ContainerSandbox())
    entry = await ts.execute("echo hi")
    assert entry["exit_code"] is None  # no process ran
    assert "unavailable" in entry["stderr"]


# ---------------------------------------------------------------------------
# 3. Spec validation parity with the process backend
# ---------------------------------------------------------------------------

async def test_cwd_outside_rejected_with_process_backend_wording(tmp_path,
                                                                 monkeypatch):
    _force_unavailable(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = tmp_path / "scope"
    scope.mkdir()
    spec = _spec(scope, workspace_writable=str(outside))
    r_c = await ContainerSandbox().run(spec)
    r_p = await ProcessSandbox().run(spec)
    assert r_c.error == r_p.error == \
        "sandbox rejected spec: cwd outside writable workspace"
    assert r_c.killed_reason == "supervisor"
    assert r_c.exit_code is None
    assert r_c.enforced == ["spec_validation"]


async def test_readable_scope_outside_rejected(tmp_path, monkeypatch):
    _force_unavailable(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = tmp_path / "scope"
    scope.mkdir()
    r = await ContainerSandbox().run(
        _spec(scope, workspace_readable=(str(outside),)))
    assert r.error == \
        "sandbox rejected spec: readable scope outside writable workspace"
    assert r.exit_code is None


def test_container_cwd_maps_inside_mount(tmp_path):
    cwd, err = map_container_cwd(_spec(tmp_path))
    assert err is None
    assert cwd == CONTAINER_WORKSPACE


def test_container_cwd_maps_subdirectory(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    cwd, err = map_container_cwd(
        _spec(sub, workspace_writable=str(tmp_path)))
    assert err is None
    assert cwd == f"{CONTAINER_WORKSPACE}/sub"


def test_container_cwd_never_climbs_out_of_mount(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = tmp_path / "scope"
    scope.mkdir()
    cwd, err = map_container_cwd(
        _spec(scope, workspace_writable=str(outside)))
    assert cwd is None
    assert err is not None and "outside writable workspace" in err.error


# ---------------------------------------------------------------------------
# 4. Run-argv construction — security properties (pure, no runtime needed)
# ---------------------------------------------------------------------------

def _run_argv(spec, tmp_path=None, user="1000:1000"):
    cwd, err = map_container_cwd(spec)
    assert err is None
    return build_run_argv(
        spec, runtime_exe="docker", image="img:tag", user=user,
        python_bin="/usr/local/bin/python3", container_cwd=cwd,
        name="raivisox-sbx-test")


def test_run_argv_security_flags(tmp_path):
    cmd = _run_argv(_spec(tmp_path))
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert "--privileged" not in cmd
    assert "--rm" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop" in cmd and cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in cmd and \
        cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert cmd[cmd.index("--user") + 1] == "1000:1000"
    assert cmd[cmd.index("--name") + 1] == "raivisox-sbx-test"
    # Runtime socket / host paths: never mounted, never mentioned.
    joined = " ".join(cmd)
    assert "docker.sock" not in joined
    assert "/var/run" not in joined


def test_run_argv_single_workspace_mount_only(tmp_path):
    cmd = _run_argv(_spec(tmp_path))
    mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c in ("-v", "--volume")]
    assert len(mounts) == 1
    assert mounts[0] == f"{os.path.abspath(str(tmp_path))}:{CONTAINER_WORKSPACE}:rw"


def test_run_argv_network_deny_is_none_never_host(tmp_path):
    cmd = _run_argv(_spec(tmp_path))  # network defaults to "deny"
    nets = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--network"]
    assert nets == ["none"]


def test_run_argv_network_allow_uses_bridge_not_host(tmp_path):
    cmd = _run_argv(_spec(tmp_path, network="allow"))
    nets = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--network"]
    assert nets == []  # runtime default bridge — --network host never appears
    assert "host" not in nets


def test_run_argv_env_is_explicit_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/host-api/bin")
    spec = SandboxSpec(
        argv=[sys.executable, "-c", "print(1)"], cwd=str(tmp_path),
        env={SECRET_KEY_NAME: SECRET_VALUE,  # not allowlisted → dropped
             "PYTHONIOENCODING": "utf-8"})
    cmd = _run_argv(spec)
    e_values = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-e"]
    assert sorted(e_values) == ["HOME=/tmp", "PYTHONIOENCODING=utf-8",
                                "TMPDIR=/tmp"]
    joined = " ".join(cmd)
    assert SECRET_VALUE not in joined
    assert f"PATH=/host-api/bin" not in joined  # host env never forwarded
    assert SECRET_KEY_NAME not in joined


def test_run_argv_never_joins_argv_into_shell_string(tmp_path):
    code = "import os; print('a; b && c | d $(x) `y` > z')"
    cmd = _run_argv(_spec(tmp_path, argv=[sys.executable, "-c", code]))
    # The code travels as ONE element, verbatim — no shell ever sees it.
    assert code in cmd
    assert cmd.count(code) == 1
    image_idx = cmd.index("img:tag")
    # image then the exact argv, element by element
    assert cmd[image_idx + 1] == "/usr/local/bin/python3"  # host python mapped
    assert cmd[image_idx + 2:] == ["-c", code]


def test_run_argv_maps_only_exact_host_interpreter(tmp_path):
    other = _run_argv(_spec(tmp_path, argv=["/usr/bin/ls", "-la"]))
    assert other[other.index("img:tag") + 1] == "/usr/bin/ls"
    assert other[-2:] == ["/usr/bin/ls", "-la"]


def test_run_argv_remap_can_be_disabled(tmp_path):
    cmd = _run_argv(_spec(tmp_path))
    # python_bin="" → no mapping, host interpreter passed through honestly
    cwd, err = map_container_cwd(_spec(tmp_path))
    cmd2 = build_run_argv(_spec(tmp_path), runtime_exe="docker", image="img:tag",
                          user="1000:1000", python_bin="", container_cwd=cwd,
                          name="raivisox-sbx-test")
    assert cmd2[cmd2.index("img:tag") + 1] == sys.executable
    assert cmd[cmd.index("img:tag") + 1] == "/usr/local/bin/python3"


def test_run_argv_limits_map_to_runtime_flags(tmp_path):
    plain = _run_argv(_spec(tmp_path))
    for flag in ("--memory", "--pids-limit", "--ulimit"):
        assert flag not in plain

    rich = _run_argv(_spec(tmp_path, memory_bytes=134_217_728, max_pids=32,
                           cpu_seconds=7, fsize_bytes=4096))
    assert rich[rich.index("--memory") + 1] == "134217728"
    assert rich[rich.index("--pids-limit") + 1] == "32"
    ulimits = [rich[i + 1] for i, c in enumerate(rich) if c == "--ulimit"]
    assert ulimits == ["cpu=7:7", "fsize=4096:4096"]


def test_container_env_never_reads_process_environ(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_KEY_NAME, SECRET_VALUE)
    monkeypatch.setenv("PATH", "/host-api/bin")
    env = build_container_env(_spec(tmp_path))
    assert env["HOME"] == "/tmp" and env["TMPDIR"] == "/tmp"
    assert SECRET_KEY_NAME not in env
    assert SECRET_VALUE not in env.values()
    assert "PATH" not in env  # spec didn't ask for PATH → host PATH not copied


# ---------------------------------------------------------------------------
# 5. Bounded output drain
# ---------------------------------------------------------------------------

async def test_read_bounded_caps_storage_but_drains_all():
    sr = asyncio.StreamReader()
    sr.feed_data(b"y" * 3_000_000)
    sr.feed_eof()
    buf, overflow = await read_bounded(sr, 1000)
    assert overflow is True
    assert len(buf) <= 1001  # storage bounded regardless of stream size
    s, truncated = decode_bounded(buf, overflow, 1000)
    assert truncated
    assert s.startswith("y" * 1000)
    assert f"[output truncated at 1000 chars]" in s


async def test_read_bounded_passthrough_small_output():
    sr = asyncio.StreamReader()
    sr.feed_data(b"hello\n")
    sr.feed_eof()
    buf, overflow = await read_bounded(sr, 1000)
    assert overflow is False
    s, truncated = decode_bounded(buf, overflow, 1000)
    assert s == "hello\n"
    assert truncated is False


# ---------------------------------------------------------------------------
# 6. Probe honesty (matches the actual host)
# ---------------------------------------------------------------------------

def test_probe_reason_matches_result():
    exe, reason = probe_runtime(force=True)
    assert (exe is None) != (reason == "")  # exactly one of them is set
    if exe is not None:
        assert os.path.exists(exe)
        assert reason == ""


# ---------------------------------------------------------------------------
# 7. Live container tests — run ONLY where a runtime AND image exist
# ---------------------------------------------------------------------------

_IMAGE = os.environ.get("SANDBOX_CONTAINER_IMAGE", "").strip() or DEFAULT_IMAGE


def _live_state() -> tuple[bool, str]:
    exe, _ = probe_runtime(force=True)
    if not exe:
        return False, "no container runtime (docker/podman) on this host"
    try:
        p = subprocess.run(  # noqa: S603 - fixed argv, trusted binary
            [exe, "image", "inspect", _IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False, "container runtime image inspect failed"
    if p.returncode != 0:
        return False, f"image {_IMAGE} not present locally (tests never pull)"
    return True, ""


_LIVE, _LIVE_REASON = _live_state()


def live(fn=None):
    """Mark a test as live-container verification and gate it honestly.

    Adds the ``live`` marker (so ``pytest -m live`` selects the set in the
    CI container gate) and skips with an explicit reason when no runtime
    AND image exist on the host. Skipped tests are never passes.
    """
    marks = [pytest.mark.live]
    if not _LIVE:
        marks.append(pytest.mark.skip(
            reason=f"live container test skipped: {_LIVE_REASON}"))

    def _deco(f):
        for m in reversed(marks):
            f = m(f)
        return f

    return _deco(fn) if fn is not None else _deco


@live
async def test_live_basic_argv_execution(tmp_path):
    sb = ContainerSandbox()
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c", "print(6*7)"]))
    assert r.success, r.error
    assert r.stdout.strip() == "42"
    assert r.killed_reason is None
    for key in ("argv_execution", "minimal_env", "fs_confinement",
                "network_deny", "non_root", "cap_drop_all",
                "no_new_privileges", "timeout_kill", "bounded_output"):
        assert key in r.enforced, r.enforced
    caps = sb.capabilities()
    assert caps["runtime_available"] is True
    assert caps["fs_confinement"] is True and caps["network_deny"] is True


@live
async def test_live_secret_and_host_env_isolation(tmp_path):
    sb = ContainerSandbox()
    code = ("import os, json; print(os.environ.get('RAIVISOX_PHASE4_SECRET', "
            "'ABSENT')); print(json.dumps(sorted(os.environ.keys())))")
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c", code]))
    assert r.success, r.error
    assert "ABSENT" in r.stdout
    assert SECRET_VALUE not in r.stdout + r.stderr
    assert f"PATH={os.environ.get('PATH', '')}" not in r.stdout


@live
async def test_live_workspace_read_write(tmp_path):
    sb = ContainerSandbox()
    code = ("import os; open('out.txt', 'w').write('landed'); "
            "print(os.getcwd())")
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c", code]))
    assert r.success, r.error
    assert (tmp_path / "out.txt").read_text() == "landed"
    assert r.stdout.strip() == CONTAINER_WORKSPACE


@live
async def test_live_host_filesystem_and_socket_invisible(tmp_path):
    """Host paths outside the mount (and the runtime socket) do not exist
    inside the container — the host root is simply not there."""
    sentinel = tmp_path.parent / f"sentinel-{os.getpid()}.txt"
    sentinel.write_text("HOST-SENTINEL-DATA")
    try:
        sb = ContainerSandbox()
        code = (
            "try:\n"
            f"    print('READ:' + open({str(sentinel)!r}).read())\n"
            "except Exception as e:\n"
            "    print('DENIED', type(e).__name__)\n"
            "try:\n"
            "    open('/var/run/docker.sock')\n"
            "    print('SOCKET-VISIBLE')\n"
            "except Exception as e:\n"
            "    print('SOCKET-DENIED', type(e).__name__)\n"
        )
        r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c", code]))
        assert r.success, r.error
        assert "HOST-SENTINEL-DATA" not in r.stdout
        assert "READ:" not in r.stdout
        assert "DENIED" in r.stdout
        assert "SOCKET-VISIBLE" not in r.stdout
        assert "SOCKET-DENIED" in r.stdout
    finally:
        sentinel.unlink(missing_ok=True)


@live
async def test_live_symlink_escape_cannot_cross_container_boundary(tmp_path):
    target = tmp_path.parent / f"symlink-target-{os.getpid()}.txt"
    target.write_text("OUTSIDE-HOST-SECRET")
    ws = tmp_path / "ws"
    ws.mkdir()
    link = ws / "link"
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privileges on this host")
    try:
        sb = ContainerSandbox()
        code = ("try:\n"
                "    print('LEAK:' + open('link').read())\n"
                "except Exception as e:\n"
                "    print('DENIED', type(e).__name__)\n")
        r = await sb.run(_spec(ws, argv=[sys.executable, "-c", code]))
        assert r.success, r.error
        assert "OUTSIDE-HOST-SECRET" not in r.stdout
        assert "LEAK:" not in r.stdout
        assert "DENIED" in r.stdout
    finally:
        target.unlink(missing_ok=True)


@live
async def test_live_timeout_kills_and_container_is_reclaimed(tmp_path):
    sb = ContainerSandbox()
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c",
                                           "import time; time.sleep(60)"],
                           timeout_s=2))
    assert not r.success
    assert r.killed_reason == "timeout"
    assert r.exit_code is None
    assert "timeout" in (r.error or "")
    # No raivisox-sbx container may survive the run:
    exe, _ = probe_runtime(force=True)
    p = subprocess.run(  # noqa: S603 - fixed argv, trusted binary
        [exe, "ps", "-a", "--filter", "name=raivisox-sbx-",
         "--format", "{{.Names}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    assert p.stdout.decode().strip() == ""


@live
async def test_live_output_is_bounded(tmp_path):
    sb = ContainerSandbox()
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c",
                                           "print('x' * 3_000_000)"],
                           output_limit_bytes=100_000))
    assert r.success, r.error
    assert len(r.stdout) <= 100_000 + 100
    assert "output_limit:stdout" in r.limits_hit


@live
async def test_live_network_deny_blocks_egress(tmp_path):
    sb = ContainerSandbox()
    code = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
            "    print('CONNECTED')\n"
            "except Exception as e:\n"
            "    print('BLOCKED', type(e).__name__)\n")
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c", code]))
    assert "CONNECTED" not in r.stdout
    assert "BLOCKED" in r.stdout  # failed fast, not timed out
    assert r.killed_reason is None


@live
async def test_live_non_root_execution(tmp_path):
    sb = ContainerSandbox()
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c",
                                           "import os; print(os.getuid())"]))
    assert r.success, r.error
    assert int(r.stdout.strip()) != 0
    assert "non_root" in r.enforced


@live
async def test_live_no_container_survives_normal_run(tmp_path):
    sb = ContainerSandbox()
    r = await sb.run(_spec(tmp_path, argv=[sys.executable, "-c", "print('ok')"]))
    assert r.success, r.error
    exe, _ = probe_runtime(force=True)
    p = subprocess.run(  # noqa: S603 - fixed argv, trusted binary
        [exe, "ps", "-a", "--filter", "name=raivisox-sbx-",
         "--format", "{{.Names}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    assert p.stdout.decode().strip() == ""
