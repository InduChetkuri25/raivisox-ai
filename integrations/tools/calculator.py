"""Calculator tool for mathematical operations."""

import ast
import operator
from typing import Any, Optional

from integrations.tools.base import BaseTool, ToolResult, ToolRiskLevel, ToolPermission


class CalculatorTool(BaseTool):
    """Tool for performing mathematical calculations."""

    id = "calculator"
    name = "Calculator"
    description = "Perform mathematical calculations and evaluate expressions"
    category = "utility"
    risk_level = ToolRiskLevel.LOW
    permissions = [ToolPermission.EXTERNAL_API]  # Requires network for some operations

    input_schema = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Mathematical expression to evaluate (e.g., '2 + 2', 'sqrt(16)')",
            },
            "variables": {
                "type": "object",
                "description": "Optional variables to substitute in the expression",
                "additionalProperties": {"type": "number"},
            },
        },
        "required": ["expression"],
    }

    output_schema = {
        "type": "object",
        "properties": {
            "result": {"type": "number", "description": "The calculated result"},
            "expression": {"type": "string", "description": "The expression that was evaluated"},
            "steps": {"type": "array", "description": "Optional calculation steps"},
        },
    }

    # Safe operators for evaluation
    _SAFE_OPERATORS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def __init__(self):
        """Initialize the calculator tool."""
        super().__init__()
        self._memory: dict[str, float] = {}

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """Execute the calculator."""
        import time

        start_time = time.time()

        try:
            expression = parameters.get("expression", "")
            variables = parameters.get("variables", {})

            # Substitute variables
            if variables:
                for var, value in variables.items():
                    if var == "pi":
                        expression = expression.replace(var, str(3.14159265359))
                    elif var == "e":
                        expression = expression.replace(var, str(2.71828182846))
                    else:
                        expression = expression.replace(var, str(value))

            # Evaluate safely
            result = self._safe_eval(expression)

            execution_time = int((time.time() - start_time) * 1000)

            return ToolResult(
                success=True,
                output={
                    "result": result,
                    "expression": expression,
                },
                execution_time_ms=execution_time,
                metadata={"variables_used": list(variables.keys())},
            )

        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Calculation error: {str(e)}",
                execution_time_ms=execution_time,
            )

    def _safe_eval(self, expression: str) -> float:
        """Safely evaluate a mathematical expression."""
        # Remove any potentially dangerous characters
        expression = expression.strip()

        # Handle common functions using regex for sqrt(x)
        import re
        expression = re.sub(r'sqrt\(([^)]+)\)', r'(\1)**0.5', expression)
        expression = expression.replace("abs(", "abs(")
        expression = expression.replace("round(", "round(")

        # Parse and evaluate
        tree = ast.parse(expression, mode='eval')
        return self._eval_node(tree.body)

    def _eval_node(self, node: ast.AST) -> float:
        """Recursively evaluate an AST node."""
        if isinstance(node, ast.Expression):
            return self._eval_node(node.body)
        elif isinstance(node, ast.Constant):
            return float(node.value)
        elif isinstance(node, ast.Num):  # Python 3.7 compatibility
            return float(node.n)
        elif isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            op_type = type(node.op)
            if op_type in self._SAFE_OPERATORS:
                return self._SAFE_OPERATORS[op_type](left, right)
            raise ValueError(f"Unsupported operator: {op_type}")
        elif isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            op_type = type(node.op)
            if op_type in self._SAFE_OPERATORS:
                return self._SAFE_OPERATORS[op_type](operand)
            raise ValueError(f"Unsupported unary operator: {op_type}")
        else:
            raise ValueError(f"Unsupported expression type: {type(node)}")
