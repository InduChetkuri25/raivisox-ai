"""Terminal sandbox tool for executing commands in a sandboxed environment."""

import asyncio
import os
import subprocess
from typing import Any

from integrations.tools.base import BaseTool, ToolResult, ToolRiskLevel, ToolPermission


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

            # Resolve working directory
            if not os.path.isabs(working_directory):
                working_directory = os.path.join(self.sandbox_root, working_directory)
            working_directory = os.path.abspath(working_directory)

            # Ensure working directory exists
            os.makedirs(working_directory, exist_ok=True)

            # Merge environment
            env = os.environ.copy()
            env.update(environment)

            # Execute command
            process = await asyncio.create_subprocess_exec(
                *command.split(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
                cwd=working_directory,
                env=env,
                preexec_fn=os.setsid,  # Create new process group
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill the process group
                try:
                    os.killpg(os.getpgid(process.pid), 9)  # SIGKILL
                except (ProcessLookupError, OSError):
                    pass
                await process.wait()

                execution_time = int((time.time() - start_time) * 1000)
                return ToolResult(
                    success=False,
                    output={
                        "command": command,
                        "exit_code": -1,
                        "stdout": stdout.decode('utf-8', errors='replace') if stdout else "",
                        "stderr": stderr.decode('utf-8', errors='replace') + "\n[Command timed out after {}s]".format(timeout),
                        "duration_ms": execution_time,
                        "success": False,
                    },
                    execution_time_ms=execution_time,
                )

            execution_time = int((time.time() - start_time) * 1000)

            stdout_text = stdout.decode('utf-8', errors='replace') if stdout else ""
            stderr_text = stderr.decode('utf-8', errors='replace') if stderr else ""
            exit_code = process.returncode

            return ToolResult(
                success=exit_code == 0,
                output={
                    "command": command,
                    "exit_code": exit_code,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
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
