"""File reader tool for reading files from the filesystem."""

import os
from typing import Any

from integrations.tools.base import BaseTool, ToolResult, ToolRiskLevel, ToolPermission
from services.security.path_containment import is_contained


class FileReaderTool(BaseTool):
    """Tool for reading files from the filesystem."""

    id = "file_reader"
    name = "File Reader"
    description = "Read the contents of a file from the filesystem"
    category = "filesystem"
    risk_level = ToolRiskLevel.LOW
    permissions = [ToolPermission.FILES_READ]

    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file to read",
            },
            "max_size": {
                "type": "integer",
                "description": "Maximum file size in bytes to read (optional)",
                "default": 1048576,  # 1MB default
            },
            "encoding": {
                "type": "string",
                "description": "File encoding (default: utf-8)",
                "default": "utf-8",
            },
        },
        "required": ["path"],
    }

    output_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The file contents"},
            "path": {"type": "string", "description": "The path of the read file"},
            "size": {"type": "integer", "description": "Size of the file in bytes"},
            "encoding": {"type": "string", "description": "Encoding used to read the file"},
        },
    }

    def __init__(self, allowed_directories: list[str] | None = None):
        """Initialize the file reader tool.

        Args:
            allowed_directories: List of directories that can be read from.
                If None, only the current working directory is allowed.
        """
        super().__init__()
        self.allowed_directories = allowed_directories or [os.getcwd()]

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """Execute the file read operation."""
        import time

        start_time = time.time()

        try:
            path = parameters.get("path", "")
            max_size = parameters.get("max_size", 1048576)
            encoding = parameters.get("encoding", "utf-8")

            # Resolve the path
            resolved_path = os.path.abspath(path)

            # Check if path is allowed
            if not self._is_path_allowed(resolved_path):
                return ToolResult(
                    success=False,
                    error=f"Access denied: {path} is not in allowed directories",
                )

            # Check if file exists
            if not os.path.exists(resolved_path):
                return ToolResult(
                    success=False,
                    error=f"File not found: {path}",
                )

            # Check if it's a file (not a directory)
            if not os.path.isfile(resolved_path):
                return ToolResult(
                    success=False,
                    error=f"Path is not a file: {path}",
                )

            # Check file size
            file_size = os.path.getsize(resolved_path)
            if file_size > max_size:
                return ToolResult(
                    success=False,
                    error=f"File too large: {file_size} bytes (max: {max_size} bytes)",
                )

            # Read the file
            with open(resolved_path, 'r', encoding=encoding) as f:
                content = f.read()

            execution_time = int((time.time() - start_time) * 1000)

            return ToolResult(
                success=True,
                output={
                    "content": content,
                    "path": path,
                    "size": file_size,
                    "encoding": encoding,
                },
                execution_time_ms=execution_time,
            )

        except UnicodeDecodeError as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Encoding error: {str(e)}. Try a different encoding.",
                execution_time_ms=execution_time,
            )
        except PermissionError as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Permission denied: {str(e)}",
                execution_time_ms=execution_time,
            )
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Error reading file: {str(e)}",
                execution_time_ms=execution_time,
            )

    def _is_path_allowed(self, path: str) -> bool:
        """Check if a path is within the allowed directories.

        SECURITY (Phase 0): symlink-resolving containment — the path's
        REAL resolution (``realpath``, case-normalized) must land inside
        an allowed directory, so a symlink planted under an allowed
        directory cannot redirect reads outside it.
        """
        return any(is_contained(path, d) for d in self.allowed_directories)
