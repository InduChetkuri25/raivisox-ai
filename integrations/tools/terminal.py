"""Terminal sandbox tool for executing commands in a sandboxed environment.

V2 Phase 3: the spawn is routed through the shared ExecutionSandbox
boundary (argv via the shell-free command resolver, minimal allowlisted
environment, Phase 0 symlink-safe cwd containment, bounded output,
timeout tree-kill, no unsandboxed fallback). The command validator and
blocklist remain an advisory pre-filter, not the boundary. Caller-supplied
``environment`` keys are passed through the sandbox's allowlist — keys it
does not allow (i.e. anything that could carry host secrets) are dropped.
"""

import os
import time
from typing import Any

from integrations.tools.base import BaseTool, ToolResult, ToolRiskLevel, ToolPermission
from services.execution.sandbox import SandboxSpec, build_default_sandbox
from services.execution.terminal_sandbox import _PWD_SENTINEL, resolve_command_argv
from services.security.path_containment import contained_path


class TerminalSandboxTool(BaseTool):
    """Tool for executing commands in a sandboxed terminal environment."""

    id = "terminal"
    name = "Terminal Sandbox"
    description = "Execute shell commands in a sandboxed environment"
    category = "system"
    risk_level = ToolRiskLevel.HIGH
    permissions = [ToolPermission.TERMINAL_EXECUTE]

    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "working_directory": {
                "type": "string",
                "description": "Working directory for command execution (default: sandbox root)",
            },
            "timeout": {
                "type": "integer",
                "description": "Command timeout in seconds (default: 60)",
                "default": 60,
            },
            "environment": {
                "type": "object",
                "description": "Additional environment variables",
                "additionalProperties": {"type": "string"},
            },
            "capture_output": {
                "type": "boolean",
                "description": "Whether to capture stdout/stderr (default: true)",
                "default": True,
            },
        },
        "required": ["command"],
    }

    output_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The executed command"},
            "exit_code": {"type": "integer", "description": "Process exit code"},
            "stdout": {"type": "string", "description": "Standard output"},
            "stderr": {"type": "string", "description": "Standard error"},
            "duration_ms": {"type": "integer", "description": "Execution duration in milliseconds"},
            "success": {"type": "boolean", "description": "Whether the command succeeded (exit code 0)"},
        },
    }

    def __init__(
        self,
        sandbox_root: str | None = None,
        allowed_commands: list[str] | None = None,
        blocked_commands: list[str] | None = None,
    ):
        """Initialize the terminal sandbox tool.

        Args:
            sandbox_root: Root directory for sandbox. If None, uses a temp directory.
            allowed_commands: List of allowed commands. If None, all commands are allowed.
            blocked_commands: List of blocked commands. If None, defaults are used.
        """
        super().__init__()
        self.sandbox_root = sandbox_root or os.path.join(os.getcwd(), ".sandbox")
        self.allowed_commands = allowed_commands
        self.blocked_commands = blocked_commands or [
            "rm", "rmdir", "dd", "mkfs", "fdisk", "shutdown", "reboot",
            "chmod", "chown", "mount", "umount", "sudo", "su",
            "curl", "wget",  # Network access disabled
            "nc", "netcat", "ssh", "scp", "telnet",
            ":(){ :& :& };:",  # Fork bomb
        ]

        # Create sandbox directory
        os.makedirs(self.sandbox_root, exist_ok=True)

        # Shared ExecutionSandbox boundary for every spawn (V2 Phase 3).
        self._exec_sandbox = build_default_sandbox()

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """Execute a command in the sandbox."""
        import time

        start_time = time.time()

        try:
            command = parameters.get("command", "").strip()
            working_directory = parameters.get("working_directory", self.sandbox_root)
            timeout = parameters.get("timeout", 60)
            environment = parameters.get("environment", {})
            capture_output = parameters.get("capture_output", True)

            # Validate command
            validation_error = self._validate_command(command)
            if validation_error:
                return ToolResult(success=False, error=validation_error)

            # V2 Phase 3: this tool previously spawned commands directly
            # (naive argv split, os.environ.copy() inheritance, POSIX-only
            # killpg). It now goes through the same ExecutionSandbox as
            # every other untrusted spawn — and NEVER falls back to an
            # unsandboxed execution.
            if not os.path.isabs(working_directory):
                working_directory = os.path.join(self.sandbox_root, working_directory)
            working_directory = os.path.abspath(working_directory)

            # Phase 0 containment: resolve symlinks before deciding the
            # working directory is inside the sandbox root.
            if not contained_path(working_directory, self.sandbox_root):
                return ToolResult(
                    success=False,
                    error=f"Working directory '{working_directory}' is outside the sandbox root",
                )
            os.makedirs(working_directory, exist_ok=True)

            chdir_target, argv, parse_err = resolve_command_argv(
                command, working_directory
            )
            if parse_err is not None:
                return ToolResult(success=False, error=parse_err)

            execution_time = int((time.time() - start_time) * 1000)

            if chdir_target == _PWD_SENTINEL:  # pwd / bare cd: no process
                return ToolResult(
                    success=True,
                    output={
                        "command": command,
                        "exit_code": 0,
                        "stdout": working_directory + "\n",
                        "stderr": "",
                        "duration_ms": execution_time,
                        "success": True,
                    },
                    execution_time_ms=execution_time,
                )
            if chdir_target is not None:  # validated `cd <dir>`: fresh-process no-op
                return ToolResult(
                    success=True,
                    output={
                        "command": command,
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "duration_ms": execution_time,
                        "success": True,
                    },
                    execution_time_ms=execution_time,
                )

            spec = SandboxSpec(
                argv=argv,
                cwd=working_directory,
                # Caller env keys go through the sandbox allowlist; anything
                # not allowlisted (host secrets) is dropped by the sandbox.
                env={str(k): str(v) for k, v in (environment or {}).items()},
                timeout_s=float(timeout),
                workspace_writable=working_directory,
            )
            result = await self._exec_sandbox.run(spec)

            execution_time = int((time.time() - start_time) * 1000)

            if result.killed_reason == "timeout":
                return ToolResult(
                    success=False,
                    output={
                        "command": command,
                        "exit_code": -1,
                        "stdout": result.stdout,
                        "stderr": result.stderr
                        + f"\n[Command timed out after {timeout}s]",
                        "duration_ms": execution_time,
                        "success": False,
                    },
                    execution_time_ms=execution_time,
                )
            if result.error is not None:
                # Sandbox/spawn failure — honest failure; never unsandboxed.
                return ToolResult(
                    success=False,
                    error=result.error,
                    execution_time_ms=execution_time,
                )

            exit_code = result.exit_code
            return ToolResult(
                success=exit_code == 0,
                output={
                    "command": command,
                    "exit_code": exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "duration_ms": execution_time,
                    "success": exit_code == 0,
                },
                execution_time_ms=execution_time,
            )

        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Error executing command: {str(e)}",
                execution_time_ms=execution_time,
            )

    def _validate_command(self, command: str) -> str | None:
        """Validate command for security."""
        if not command:
            return "Empty command"

        # Check for blocked commands
        command_parts = command.split()
        if command_parts:
            base_cmd = os.path.basename(command_parts[0])
            if base_cmd in self.blocked_commands:
                return f"Command '{base_cmd}' is not allowed for security reasons"

        # Check for shell metacharacters that could be dangerous
        dangerous_patterns = [
            ";", "&&", "||", "|", "`", "$", ">", "<", "&",
        ]
        for pattern in dangerous_patterns:
            if pattern in command:
                # Allow some patterns in controlled ways
                if pattern in (">", "<") and ">>" not in command:
                    return f"Dangerous pattern '{pattern}' detected"

        # Limit command length
        if len(command) > 1000:
            return "Command too long (max 1000 characters)"

        return None

    def reset_sandbox(self):
        """Reset the sandbox to a clean state."""
        import shutil
        if os.path.exists(self.sandbox_root):
            shutil.rmtree(self.sandbox_root)
        os.makedirs(self.sandbox_root, exist_ok=True)
