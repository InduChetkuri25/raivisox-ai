"""Unit tests for tools."""

import pytest
import asyncio


class TestCalculatorTool:
    """Tests for CalculatorTool."""

    @pytest.fixture
    def calculator(self):
        from integrations.tools.calculator import CalculatorTool
        return CalculatorTool()

    @pytest.mark.asyncio
    async def test_simple_addition(self, calculator):
        """Calculator should perform simple addition."""
        result = await calculator.execute({"expression": "2 + 2"})
        assert result.success is True
        assert result.output["result"] == 4.0

    @pytest.mark.asyncio
    async def test_subtraction(self, calculator):
        """Calculator should perform subtraction."""
        result = await calculator.execute({"expression": "10 - 3"})
        assert result.success is True
        assert result.output["result"] == 7.0

    @pytest.mark.asyncio
    async def test_multiplication(self, calculator):
        """Calculator should perform multiplication."""
        result = await calculator.execute({"expression": "4 * 5"})
        assert result.success is True
        assert result.output["result"] == 20.0

    @pytest.mark.asyncio
    async def test_division(self, calculator):
        """Calculator should perform division."""
        result = await calculator.execute({"expression": "20 / 4"})
        assert result.success is True
        assert result.output["result"] == 5.0

    @pytest.mark.asyncio
    async def test_complex_expression(self, calculator):
        """Calculator should handle complex expressions."""
        result = await calculator.execute({"expression": "(2 + 3) * 4"})
        assert result.success is True
        assert result.output["result"] == 20.0

    @pytest.mark.asyncio
    async def test_power(self, calculator):
        """Calculator should handle power operations."""
        result = await calculator.execute({"expression": "2 ** 3"})
        assert result.success is True
        assert result.output["result"] == 8.0

    @pytest.mark.asyncio
    async def test_sqrt(self, calculator):
        """Calculator should handle square root."""
        result = await calculator.execute({"expression": "sqrt(16)"})
        assert result.success is True
        assert result.output["result"] == 4.0

    @pytest.mark.asyncio
    async def test_invalid_expression(self, calculator):
        """Calculator should handle invalid expressions gracefully."""
        result = await calculator.execute({"expression": "invalid"})
        assert result.success is False
        assert result.error is not None


class TestFileReaderTool:
    """Tests for FileReaderTool."""

    @pytest.fixture
    def file_reader(self, tmp_path):
        from integrations.tools.file_reader import FileReaderTool
        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, RAIVISOX!")
        return FileReaderTool(allowed_directories=[str(tmp_path)])

    @pytest.mark.asyncio
    async def test_read_existing_file(self, file_reader, tmp_path):
        """FileReader should read existing files."""
        test_file = tmp_path / "read_test.txt"
        test_file.write_text("Test content")

        result = await file_reader.execute({
            "path": str(test_file),
        })

        assert result.success is True
        assert result.output["content"] == "Test content"
        assert result.output["size"] > 0

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, file_reader):
        """FileReader should fail gracefully for nonexistent files."""
        result = await file_reader.execute({
            "path": "/nonexistent/path/file.txt",
        })

        assert result.success is False

    @pytest.mark.asyncio
    async def test_read_outside_allowed_directory(self, file_reader):
        """FileReader should reject paths outside allowed directories."""
        result = await file_reader.execute({
            "path": "/etc/passwd",
        })

        assert result.success is False


class TestDirectoryTool:
    """Tests for DirectoryTool."""

    @pytest.fixture
    def directory_tool(self, tmp_path):
        from integrations.tools.directory import DirectoryTool
        return DirectoryTool(allowed_directories=[str(tmp_path)])

    @pytest.mark.asyncio
    async def test_list_directory(self, directory_tool, tmp_path):
        """DirectoryTool should list directory contents."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")
        (tmp_path / "subdir").mkdir()

        result = await directory_tool.execute({
            "path": str(tmp_path),
            "include_files": True,
            "include_dirs": True,
        })

        assert result.success is True
        assert result.output["total"] >= 3  # 2 files + 1 dir
        assert len(result.output["items"]) >= 3

    @pytest.mark.asyncio
    async def test_list_nonexistent_directory(self, directory_tool):
        """DirectoryTool should fail for nonexistent directories."""
        result = await directory_tool.execute({
            "path": "/nonexistent/path",
        })

        assert result.success is False


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_register_and_get_tool(self):
        """Registry should allow registering and retrieving tools."""
        from integrations.tools.base import ToolRegistry, ToolPermission, ToolRiskLevel, BaseTool

        class TestTool(BaseTool):
            id = "test_tool"
            name = "Test Tool"
            description = "A test tool"
            category = "test"
            risk_level = ToolRiskLevel.LOW
            permissions = [ToolPermission.EXTERNAL_API]

            async def execute(self, parameters):
                return {"result": "success"}

        registry = ToolRegistry()
        tool = TestTool()
        registry.register(tool)

        retrieved = registry.get("test_tool")
        assert retrieved is not None
        assert retrieved.id == "test_tool"

    def test_list_tools(self):
        """Registry should list all registered tools."""
        from integrations.tools.base import ToolRegistry, ToolPermission, ToolRiskLevel, BaseTool

        class Tool1(BaseTool):
            id = "tool1"
            name = "Tool 1"
            description = "First test tool"
            category = "test"
            risk_level = ToolRiskLevel.LOW
            permissions = [ToolPermission.EXTERNAL_API]

            async def execute(self, parameters):
                return {}

        class Tool2(BaseTool):
            id = "tool2"
            name = "Tool 2"
            description = "Second test tool"
            category = "test"
            risk_level = ToolRiskLevel.MEDIUM
            permissions = [ToolPermission.FILES_READ]

            async def execute(self, parameters):
                return {}

        registry = ToolRegistry()
        registry.register(Tool1())
        registry.register(Tool2())

        tools = registry.list_tools()
        assert len(tools) == 2
        assert tools[0]["id"] == "tool1"
        assert tools[1]["id"] == "tool2"

    def test_get_by_permission(self):
        """Registry should filter tools by permission."""
        from integrations.tools.base import (
            ToolRegistry, ToolPermission, ToolRiskLevel, BaseTool
        )

        class ReadTool(BaseTool):
            id = "read_tool"
            name = "Read Tool"
            description = "A read tool"
            category = "test"
            risk_level = ToolRiskLevel.LOW
            permissions = [ToolPermission.FILES_READ]

            async def execute(self, parameters):
                return {}

        class WriteTool(BaseTool):
            id = "write_tool"
            name = "Write Tool"
            description = "A write tool"
            category = "test"
            risk_level = ToolRiskLevel.MEDIUM
            permissions = [ToolPermission.FILES_WRITE]

            async def execute(self, parameters):
                return {}

        registry = ToolRegistry()
        registry.register(ReadTool())
        registry.register(WriteTool())

        read_tools = registry.get_by_permission(ToolPermission.FILES_READ)
        assert len(read_tools) == 1
        assert read_tools[0].id == "read_tool"
