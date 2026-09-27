"""Tool adapters for the RAIVISOX execution pipeline."""

from integrations.adapters.base import (
    CAPABILITY_TO_ADAPTER,
    DEFAULT_ADAPTER_ID,
    AdapterContext,
    AdapterResult,
    ToolAdapter,
)
from integrations.adapters.builtin import (
    FileReadAdapter,
    FileWriteAdapter,
    PythonExecAdapter,
    ShellExecAdapter,
    TextTransformAdapter,
)
from integrations.adapters.http_get import HttpGetAdapter
from integrations.adapters.text_intel import TextIntelAdapter
from integrations.adapters.finalize import FinalizeAdapter
from integrations.adapters.codegen import CodegenAdapter
from integrations.adapters.registry import AdapterRegistry, UnknownToolError

__all__ = [
    "CAPABILITY_TO_ADAPTER",
    "AdapterContext",
    "AdapterResult",
    "ToolAdapter",
    "AdapterRegistry",
    "UnknownToolError",
    "FileReadAdapter",
    "FileWriteAdapter",
    "PythonExecAdapter",
    "ShellExecAdapter",
    "TextTransformAdapter",
    "HttpGetAdapter",
    "TextIntelAdapter",
    "FinalizeAdapter",
    "CodegenAdapter",
    "DEFAULT_ADAPTER_ID",
]
