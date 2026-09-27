"""Directory listing tool for browsing filesystem directories."""

import os
from datetime import datetime
from typing import Any

from integrations.tools.base import BaseTool, ToolResult, ToolRiskLevel, ToolPermission


class DirectoryTool(BaseTool):
    """Tool for listing directory contents."""

    id = "directory"
    name = "Directory Lister"
    description = "List the contents of a directory"
    category = "filesystem"
    risk_level = ToolRiskLevel.LOW
    permissions = [ToolPermission.FILES_READ]

    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the directory to list",
            },
            "recursive": {
                "type": "boolean",
                "description": "Whether to list contents recursively (default: false)",
                "default": False,
            },
            "max_depth": {
                "type": "integer",
                "description": "Maximum recursion depth when recursive=true (default: 3)",
                "default": 3,
            },
            "include_files": {
                "type": "boolean",
                "description": "Whether to include files in the listing (default: true)",
                "default": True,
            },
            "include_dirs": {
                "type": "boolean",
                "description": "Whether to include directories in the listing (default: true)",
                "default": True,
            },
        },
        "required": ["path"],
    }

    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path that was listed"},
            "items": {
                "type": "array",
                "description": "List of items in the directory",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                        "type": {"type": "string", "enum": ["file", "directory"]},
                        "size": {"type": "integer"},
                        "modified": {"type": "string"},
                    },
                },
            },
            "total": {"type": "integer", "description": "Total number of items"},
        },
    }

    def __init__(self, allowed_directories: list[str] | None = None):
        """Initialize the directory tool.

        Args:
            allowed_directories: List of directories that can be listed.
                If None, only the current working directory is allowed.
        """
        super().__init__()
        self.allowed_directories = allowed_directories or [os.getcwd()]

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """Execute the directory listing."""
        import time

        start_time = time.time()

        try:
            path = parameters.get("path", ".")
            recursive = parameters.get("recursive", False)
            max_depth = parameters.get("max_depth", 3)
            include_files = parameters.get("include_files", True)
            include_dirs = parameters.get("include_dirs", True)

            # Resolve the path
            resolved_path = os.path.abspath(path)

            # Check if path is allowed
            if not self._is_path_allowed(resolved_path):
                return ToolResult(
                    success=False,
                    error=f"Access denied: {path} is not in allowed directories",
                )

            # Check if path exists
            if not os.path.exists(resolved_path):
                return ToolResult(
                    success=False,
                    error=f"Directory not found: {path}",
                )

            # Check if it's a directory
            if not os.path.isdir(resolved_path):
                return ToolResult(
                    success=False,
                    error=f"Path is not a directory: {path}",
                )

            # List directory contents
            items = []
            if recursive:
                self._list_recursive(resolved_path, items, include_files, include_dirs, 0, max_depth)
            else:
                self._list_directory(resolved_path, items, include_files, include_dirs)

            # Sort items by name
            items.sort(key=lambda x: (not x["type"] == "directory", x["name"].lower()))

            execution_time = int((time.time() - start_time) * 1000)

            return ToolResult(
                success=True,
                output={
                    "path": path,
                    "items": items,
                    "total": len(items),
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
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Error listing directory: {str(e)}",
                execution_time_ms=execution_time,
            )

    def _list_directory(self, path: str, items: list, include_files: bool, include_dirs: bool):
        """List a single directory."""
        try:
            for entry in os.scandir(path):
                if entry.is_dir():
                    if include_dirs:
                        stat = entry.stat()
                        items.append({
                            "name": entry.name,
                            "path": entry.path,
                            "type": "directory",
                            "size": stat.st_size,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        })
                else:
                    if include_files:
                        stat = entry.stat()
                        items.append({
                            "name": entry.name,
                            "path": entry.path,
                            "type": "file",
                            "size": stat.st_size,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        })
        except PermissionError:
            pass

    def _list_recursive(self, path: str, items: list, include_files: bool, include_dirs: bool, depth: int, max_depth: int):
        """Recursively list directory contents."""
        if depth > max_depth:
            return

        self._list_directory(path, items, include_files, include_dirs)

        # Recurse into subdirectories
        try:
            for entry in os.scandir(path):
                if entry.is_dir():
                    try:
                        self._list_recursive(
                            entry.path, items, include_files, include_dirs,
                            depth + 1, max_depth
                        )
                    except PermissionError:
                        pass
        except PermissionError:
            pass

    def _is_path_allowed(self, path: str) -> bool:
        """Check if a path is within the allowed directories."""
        for allowed_dir in self.allowed_directories:
            allowed_abs = os.path.abspath(allowed_dir)
            # Check if path starts with allowed directory
            if path.startswith(allowed_abs + os.sep) or path == allowed_abs:
                return True
        return False
