"""Controlled terminal execution environment."""

import asyncio
import time


class TerminalSandbox:
    """Sandboxed terminal execution with command allow/block lists."""

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir
        self._allow_list: list[str] = []
        self._block_list: list[str] = [
            "rm -rf /",
            "chmod 777 /",
            ":(){ :|:& };:",
            "mkfs.",
            "dd if=",
        ]
        self._command_history: list[dict] = []

    def allow_command(self, pattern: str) -> None:
        self._allow_list.append(pattern)

    def block_command(self, pattern: str) -> None:
        self._block_list.append(pattern)

    def is_allowed(self, command: str) -> tuple[bool, str]:
        cmd_lower = command.lower().strip()
        for blocked in self._block_list:
            if blocked.lower() in cmd_lower:
                return False, f"Command blocked: matches blocked pattern '{blocked}'"
        if self._allow_list:
            for allowed in self._allow_list:
                if allowed.lower() in cmd_lower:
                    return True, "Allowed"
            return False, f"Command not in allow list. Allowed patterns: {self._allow_list}"
        return True, "Allowed (no restrictions)"

    async def execute(self, command: str, timeout: int = 30) -> dict:
        """Execute a command in the sandbox with timeout and history tracking."""
        allowed, reason = self.is_allowed(command)
        entry = {
            "command": command,
            "allowed": allowed,
            "reason": reason,
            "timestamp": time.time(),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
        }
        self._command_history.append(entry)

        if not allowed:
            entry["stdout"] = f"BLOCKED: {reason}"
            return entry

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                entry["stdout"] = stdout.decode(errors="replace")
                entry["stderr"] = stderr.decode(errors="replace")
                entry["exit_code"] = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                entry["stdout"] = ""
                entry["stderr"] = f"Command timed out after {timeout}s"
                entry["exit_code"] = -1
        except Exception as e:
            entry["stderr"] = str(e)
            entry["exit_code"] = -1

        return entry

    @property
    def command_history(self) -> list[dict]:
        return list(self._command_history)
