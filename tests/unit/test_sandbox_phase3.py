"""V2 Phase 3 — shell_exec / TerminalSandbox through the ExecutionSandbox.

Focused tests for the shell migration (docs/sandbox-design.md §7 phase 4):
the last ``create_subprocess_shell`` site is gone. Command strings are
parsed into explicit argv (builtins emulated, externals which-resolved)
and every real spawn goes through the sandbox boundary.

Proven here, without mocking the real execution:
- argv-only execution — no shell anywhere (the spec layer has no shell
  field; the argv resolver never joins tokens back into a shell line);
- shell metacharacters are inert DATA: ``; && | > $( )`` cannot chain,
  pipe, redirect, or substitute;
- the child environment is the sandbox's minimal allowlist — API-process
  secrets never appear;
- cwd containment via the Phase 0 symlink-resolving boundary;
- timeout kills, output bounded, honest sandbox-failure semantics, no
  unsandboxed fallback (canary-proof);
- legacy contracts preserved: entry/output shapes, "Command timed out
  after Ns" wording, allowlist blocking, per-task scoping.
"""

import asyncio
import os
import sys

import pytest

from integrations.adapters.base import AdapterContext
from integrations.adapters.builtin import ShellExecAdapter
from services.execution.sandbox import (
    ExecutionSandbox,
    ProcessSandbox,
    SandboxResult,
    SandboxSpec,
)
from services.execution.terminal_sandbox import (
    TerminalSandbox,
    resolve_command_argv,
)
from services.security.path_containment import contained_path

SECRET_NAME = "RAIVISOX_PHASE3_SECRET"
SECRET_VALUE = "phase3-secret-do-not-leak-88"


def _ctx(task="t1") -> AdapterContext:
    return AdapterContext(actor_id="tester", task_id=task, step_id="s1",
                          resource=f"task:{task}")


@pytest.fixture(autouse=True)
def _secret_env():
    os.environ[SECRET_NAME] = SECRET_VALUE
    yield
    os.environ.pop(SECRET_NAME, None)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _adapter(workspace, **kw) -> ShellExecAdapter:
    return ShellExecAdapter(str(workspace), **kw)


def _ts(workspace, **kw) -> TerminalSandbox:
    sb = TerminalSandbox(working_dir=str(workspace), **kw)
    for cmd in ("echo", "python", "cd", "pwd", "whoami"):
        sb.allow_command(cmd)
    return sb


# ---------------------------------------------------------------------------
# 1. Normal allowed commands + legacy behavior preserved
# ---------------------------------------------------------------------------

async def test_echo_allowed_and_output_captured(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": "echo sandbox-alive"}, _ctx())
    assert r.success, r.error
    assert "sandbox-alive" in r.output["stdout"]
    assert r.output["exit_code"] == 0


async def test_python_command_via_argv(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": 'python -c "print(21*2)"'}, _ctx())
    assert r.success, r.error
    assert "42" in r.output["stdout"]


async def test_pwd_prints_task_scope(workspace):
    sb = _ts(workspace)
    e = await sb.execute("pwd", timeout=10)
    assert e["exit_code"] == 0
    assert contained_path(e["stdout"].strip(), str(workspace))


async def test_cd_bare_succeeds_and_cd_dir_validated(workspace):
    sb = _ts(workspace)
    e = await sb.execute("cd", timeout=10)
    assert e["exit_code"] == 0  # bare cd succeeds (prints cwd on POSIX shells)

    (workspace / "sub").mkdir()
    e2 = await sb.execute("cd sub", timeout=10)
    assert e2["exit_code"] == 0

    e3 = await sb.execute("cd no_such_dir_xyz", timeout=10)
    assert e3["exit_code"] != 0
    assert "no such directory" in e3["stderr"]


async def test_unlisted_command_denied_not_executed(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": "del important.txt"}, _ctx())
    assert not r.success
    assert not (workspace / "important.txt").exists()


async def test_shell_exec_runs_inside_task_scope(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": "pwd"}, _ctx("task-abc"))
    assert r.success, r.error
    assert contained_path(r.output["stdout"].strip(),
                          str(workspace / "task-abc"))


# ---------------------------------------------------------------------------
# 2. Metacharacters are data — injection resistance
# ---------------------------------------------------------------------------

async def test_semicolon_cannot_chain(workspace):
    """`echo a; del x` must be ONE echo with literal tokens — no second cmd."""
    a = _adapter(workspace)
    r = await a.run({"command": "echo a; del secret.txt"}, _ctx())
    assert r.success, r.error
    assert "a; del secret.txt" in r.output["stdout"]
    assert not (workspace / "secret.txt").exists()


async def test_and_or_pipe_cannot_chain(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": "echo one && echo two | echo three"}, _ctx())
    assert r.success, r.error
    # Everything after `echo` is inert argument text, never re-interpreted.
    assert "one && echo two | echo three" in r.output["stdout"]


async def test_command_substitution_not_executed(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": "echo $(pwd) `pwd`"}, _ctx())
    assert r.success, r.error
    out = r.output["stdout"]
    assert "$(pwd)" in out and "`pwd`" in out


async def test_redirection_is_not_applied(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": "echo data > out.txt"}, _ctx())
    assert r.success, r.error
    assert "data > out.txt" in r.output["stdout"]
    assert not (workspace / "out.txt").exists()  # `>` was argument text


async def test_absolute_path_outside_workspace_is_ordinary_argument(workspace):
    """Metacharacters and absolute paths cannot make the sandbox act on
    anything outside the scope: the command is one echo, nothing more."""
    a = _adapter(workspace)
    r = await a.run({"command": f"echo {os.path.abspath('C:/')} > /etc/x"}, _ctx())
    # Allowed (echo is allowlisted) and harmless — no file was written to /
    assert r.success, r.error
    assert ">" in r.output["stdout"]


# ---------------------------------------------------------------------------
# 3. Environment isolation
# ---------------------------------------------------------------------------

async def test_env_secret_isolation(workspace):
    a = _adapter(workspace)
    r = await a.run(
        {"command": 'python -c "import os; print(os.environ.get(\'RAIVISOX_PHASE3_SECRET\', \'ABSENT\'))"'},
        _ctx())
    assert r.success, r.error
    assert r.output["stdout"].strip() == "ABSENT"
    assert SECRET_VALUE not in r.output["stdout"] + r.output["stderr"]


async def test_ts_env_secret_isolation(workspace):
    sb = _ts(workspace)
    code = "import os; print(os.environ.get('RAIVISOX_PHASE3_SECRET', 'ABSENT'))"
    e = await sb.execute(f'python -c "{code}"', timeout=15)
    assert e["exit_code"] == 0
    assert e["stdout"].strip() == "ABSENT"


# ---------------------------------------------------------------------------
# 4. Containment — cwd + symlink escape rejection
# ---------------------------------------------------------------------------

async def test_cwd_is_task_scope(workspace):
    sb = _ts(workspace)
    e = await sb.execute("python -c \"import os; print(os.getcwd())\"", timeout=15)
    assert e["exit_code"] == 0
    assert contained_path(e["stdout"].strip(), str(workspace))


async def test_cd_traversal_outside_rejected(workspace):
    sb = _ts(workspace)
    e = await sb.execute("cd ..", timeout=10)
    assert e["exit_code"] != 0
    assert "outside the workspace" in e["stderr"]


async def test_cd_symlink_outside_rejected(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "linkout"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privileges on this host")
    if not contained_path(str(link), str(workspace)):
        pytest.skip("host does not resolve symlink (Windows unprivileged)")
    sb = _ts(workspace)
    e = await sb.execute("cd linkout", timeout=10)
    assert e["exit_code"] != 0
    assert "outside the workspace" in e["stderr"]


async def test_sandbox_spec_rejects_symlinked_cwd(workspace, tmp_path):
    """ExecutionSandbox's own boundary also refuses a symlinked cwd."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "l2"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privileges on this host")
    if not contained_path(str(link), str(workspace)):
        pytest.skip("host does not resolve symlink (Windows unprivileged)")
    sb = ProcessSandbox()
    r = await sb.run(SandboxSpec(argv=[sys.executable, "-c", "print(1)"],
                                 cwd=str(link), env={}))
    assert not r.success
    assert "outside writable workspace" in r.error


# ---------------------------------------------------------------------------
# 5. Timeout, bounded output, argv resolution
# ---------------------------------------------------------------------------

async def test_timeout_kills_with_legacy_wording(workspace):
    a = _adapter(workspace)
    r = await a.run({"command": "python -c \"import time; time.sleep(60)\"",
                     "timeout": 2}, _ctx())
    assert not r.success
    assert "Command timed out after 2s" in (r.error or "") or \
           "Command timed out after 2s" in r.output["stderr"]


async def test_bounded_output(workspace):
    sb = _ts(workspace)
    e = await sb.execute("python -c \"print('x' * 5_000_000)\"", timeout=30)
    assert e["exit_code"] == 0
    assert len(e["stdout"]) < 2_000_000  # 1MB default bound held


async def test_resolver_rejects_unknown_program(workspace):
    chdir, argv, err = resolve_command_argv("definitely-not-real-xyz --x",
                                            str(workspace))
    assert argv is None
    assert err is not None and "command not found" in err


async def test_resolver_rejects_bad_quoting(workspace):
    chdir, argv, err = resolve_command_argv('echo "unterminated', str(workspace))
    assert argv is None
    assert err is not None and "parse error" in err


async def test_resolver_makes_argv_not_shell(workspace):
    chdir, argv, err = resolve_command_argv('python -c "print(1)" extra',
                                            str(workspace))
    assert chdir is None and err is None
    assert isinstance(argv, list) and all(isinstance(t, str) for t in argv)
    assert argv[0].lower().endswith(("python.exe", "python"))
    assert any("print(1)" in t for t in argv)


# ---------------------------------------------------------------------------
# 6. Sandbox unavailable — honest failure, NO unsandboxed fallback
# ---------------------------------------------------------------------------

class BrokenSandbox(ExecutionSandbox):
    async def run(self, spec: SandboxSpec) -> SandboxResult:
        return SandboxResult(error="sandbox backend unavailable",
                             killed_reason="supervisor", enforced=[])

    def capabilities(self) -> dict[str, bool]:
        return {"argv_execution": False}


async def test_shell_sandbox_unavailable_fails_step(workspace):
    canary = workspace / "canary.txt"
    sb = TerminalSandbox(working_dir=str(workspace),
                         exec_sandbox=BrokenSandbox())
    sb.allow_command("python")
    e = await sb.execute(
        f'python -c "open({str(canary)!r}, \'w\').write(\'ran\')"', timeout=10)
    assert e["exit_code"] is None  # nothing ran
    assert "sandbox backend unavailable" in e["stderr"]
    assert not canary.exists()


async def test_adapter_sandbox_unavailable_fails_step(workspace):
    a = ShellExecAdapter(str(workspace), exec_sandbox=BrokenSandbox())
    r = await a.run({"command": "echo hello"}, _ctx())
    assert not r.success
    assert "sandbox backend unavailable" in (r.error or "")


# ---------------------------------------------------------------------------
# 7. Enforcement honesty + cleanup
# ---------------------------------------------------------------------------

async def test_enforcement_reporting_matches_host(workspace):
    sb = _ts(workspace)
    e = await sb.execute("echo hi", timeout=10)
    caps = ProcessSandbox().capabilities()
    enforced = e.get("sandbox_enforced", [])
    for key in ("argv_execution", "minimal_env", "working_dir", "bounded_output"):
        assert key in enforced
    if not caps["memory_limit"]:
        # The spec asks for no rlimits; the result must not claim them.
        assert "memory_limit" not in enforced


async def test_command_history_records_all_attempts(workspace):
    sb = _ts(workspace)
    await sb.execute("echo one", timeout=10)
    await sb.execute("del everything", timeout=10)  # blocked
    await sb.execute("cd ..", timeout=10)  # parse-level refusal
    hist = sb.command_history
    assert len(hist) == 3
    assert hist[0]["exit_code"] == 0
    assert hist[1]["allowed"] is False
    assert hist[2]["exit_code"] == 1


async def test_child_process_cleanup_on_timeout(workspace):
    """After a timeout kill, the child is reaped (no zombie) and the event
    loop stays usable for the next execution."""
    sb = _ts(workspace)
    e = await sb.execute('python -c "import time; time.sleep(60)"', timeout=1)
    assert e["exit_code"] == -1
    e2 = await sb.execute("echo still-working", timeout=10)
    assert e2["exit_code"] == 0 and "still-working" in e2["stdout"]
