"""File writer tool for writing files to the filesystem."""

import os
from typing import Any

from integrations.tools.base import BaseTool, ToolResult, ToolRiskLevel, ToolPermission
from services.security.path_containment import is_contained


class FileWriterTool(BaseTool):
    """Tool for writing files to the filesystem."""

    id = "file_writer"
    name = "File Writer"
    description = "Write content to a file on the filesystem"
    category = "filesystem"
    risk_level = ToolRiskLevel.MEDIUM
    permissions = [ToolPermission.FILES_WRITE]

    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path where the file should be written",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
            "encoding": {
                "type": "string",
                "description": "File encoding (default: utf-8)",
                "default": "utf-8",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Whether to overwrite existing files (default: false)",
                "default": False,
            },
            "create_directories": {
                "type": "boolean",
                "description": "Whether to create parent directories if they don't exist (default: true)",
                "default": True,
            },
        },
        "required": ["path", "content"],
    }

    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path where the file was written"},
            "size": {"type": "integer", "description": "Size of the written file in bytes"},
            "created": {"type": "boolean", "description": "Whether a new file was created"},
            "overwritten": {"type": "boolean", "description": "Whether an existing file was overwritten"},
        },
    }

    def __init__(self, allowed_directories: list[str] | None = None):
        """Initialize the file writer tool.

        Args:
            allowed_directories: List of directories that can be written to.
                If None, only the current working directory is allowed.
        """
        super().__init__()
        self.allowed_directories = allowed_directories or [os.getcwd()]

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """Execute the file write operation."""
        import time

        start_time = time.time()

        try:
            path = parameters.get("path", "")
            content = parameters.get("content", "")
            encoding = parameters.get("encoding", "utf-8")
            overwrite = parameters.get("overwrite", False)
            create_directories = parameters.get("create_directories", True)

            # Resolve the path
            resolved_path = os.path.abspath(path)

            # Check if path is allowed
            if not self._is_path_allowed(resolved_path):
                return ToolResult(
                    success=False,
                    error=f"Access denied: {path} is not in allowed directories",
                )

            # Check if file exists
            file_exists = os.path.exists(resolved_path)

            if file_exists and not overwrite:
                return ToolResult(
                    success=False,
                    error=f"File already exists: {path}. Set overwrite=true to replace it.",
                )

            # Create parent directories if needed
            parent_dir = os.path.dirname(resolved_path)
            if create_directories and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)

            # Write the file ATOMICALLY: a concurrent reader (another
            # step's compile check or solve probe on a shared generated
            # module) must never observe a truncated file. Write to a
            # temp sibling, then os.replace (atomic on the same volume).
            import tempfile

            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=parent_dir, prefix=".raivisox_write_", suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, 'w', encoding=encoding) as f:
                    f.write(content)
                os.replace(tmp_path, resolved_path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            file_size = os.path.getsize(resolved_path)

            execution_time = int((time.time() - start_time) * 1000)

            return ToolResult(
                success=True,
                output={
                    "path": path,
                    "size": file_size,
                    "created": not file_exists,
                    "overwritten": file_exists and overwrite,
                },
                execution_time_ms=execution_time,
            )

        except PermissionError as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Permission denied: {str(e)}",
                execution_time_ms=execution_time,
            )
        except OSError as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"OS error: {str(e)}",
                execution_time_ms=execution_time,
            )
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Error writing file: {str(e)}",
                execution_time_ms=execution_time,
            )

    def _is_path_allowed(self, path: str) -> bool:
        """Check if a path is within the allowed directories.

        SECURITY (Phase 0): symlink-resolving containment — the path's
        REAL resolution (``realpath``, case-normalized) must land inside
        an allowed directory, so a symlink planted under an allowed
        directory cannot redirect writes outside it.
        """
        return any(is_contained(path, d) for d in self.allowed_directories)
