"""Executable Definition-of-Done checks for generated modules.

Bridges the declarative DoD service (services/dod/dod.py, which only
DESCRIBES criteria) to real verification: it derives executable
acceptance checks from the task goal, RUNS the generated module's
solve() in a fresh subprocess, and evaluates the real output.

Honesty contract:
- Every check records the actual output it saw, never a placeholder.
- Expectations are computed independently of the module under test
  (e.g. math.factorial(6)) — the module can never pass by echoing its
  own claim.
- "Execution succeeded" and "goal satisfied" are separate fields; a
  module can run cleanly and still fail its acceptance checks.
- When nothing executable can be derived from the goal, the service
  abstains and says so — it never invents a pass.
"""

import ast
import json
import math
import operator
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

# Binary operators we can safely evaluate for expectation expressions.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


def _safe_eval(node: ast.AST) -> float:
    """Evaluate a pure arithmetic AST (numbers, + - * / ** % //)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _safe_eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    raise ValueError("unsupported expression")


def _eval_arithmetic(expr: str) -> float | None:
    """Safely evaluate an arithmetic expression like '5 * 4 * 3 * 2 * 1'."""
    try:
        tree = ast.parse(expr, mode="eval")
        return _safe_eval(tree.body)
    except (SyntaxError, ValueError):
        return None


@dataclass
class DoDCheckResult:
    """Result of running the executable DoD checks for one module."""

    module: str
    executed: bool = False          # solve() ran without crashing
    execution_output: Any = None    # the REAL value solve() returned
    execution_error: str | None = None
    satisfied: bool = False         # the GOAL is actually met
    checks: list[dict[str, Any]] = field(default_factory=list)
    abstained: bool = False         # nothing derivable from the goal
    abstain_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "executed": self.executed,
            "execution_output": self.execution_output,
            "execution_error": self.execution_error,
            "satisfied": self.satisfied,
            "checks": self.checks,
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
        }


def derive_checks(goal: str) -> list[dict[str, Any]]:
    """Derive executable acceptance checks from the task goal text.

    Recognized patterns (each independently verifiable):
      - "factorial of N"              -> expected = math.factorial(N)
      - "verify the result is EXPR"   -> expected = evaluated EXPR
      - "product of a * b * ..."      -> expected = evaluated expression
      - "sum of a + b + ..."          -> expected = evaluated expression
      - "X squared" / "square of X"   -> expected = X ** 2
    Anything else yields no checks — the caller must abstain, not guess.
    """
    goal_l = (goal or "").lower()
    checks: list[dict[str, Any]] = []

    def _num(token: str) -> int | None:
        token = token.strip()
        if token.isdigit():
            return int(token)
        return _NUM_WORDS.get(token)

    # 1. factorial of N
    m = re.search(r"factorial\s+(?:of\s+)?(\w+)", goal_l)
    if m:
        n = _num(m.group(1))
        if n is not None and 0 <= n <= 20:
            checks.append({
                "id": "factorial_value",
                "description": f"solve() result equals {n}! (= {math.factorial(n)})",
                "kind": "value_equals",
                "expected": math.factorial(n),
            })

    # 2. explicit expectation: "verify the result is <expr>"
    m = re.search(
        r"(?:verify|check|confirm)\s+(?:that\s+)?(?:the\s+)?(?:result|output)\s+is\s+([0-9\s\.\+\-\*\/\%]+?)(?:[.,;]|$)",
        goal_l,
    )
    if m:
        expr = m.group(1).strip()
        expected = _eval_arithmetic(expr)
        if expected is not None:
            checks.append({
                "id": "expected_result",
                "description": f"solve() result equals {expr.strip()} (= {_fmt(expected)})",
                "kind": "value_equals",
                "expected": expected,
            })

    # 3. stated product/sum: "product of 5 * 4 * 3 * 2 * 1"
    m = re.search(
        r"(product|sum)\s+of\s+([0-9\s\.\+\-\*\/\%]+?)(?:[.,;]|$)",
        goal_l,
    )
    if m:
        expr = m.group(2).strip()
        expected = _eval_arithmetic(expr)
        if expected is not None:
            checks.append({
                "id": f"stated_{m.group(1)}",
                "description": f"solve() result equals {expr} (= {_fmt(expected)})",
                "kind": "value_equals",
                "expected": expected,
            })

    # 4. squares
    m = re.search(r"(?:square\s+of\s+(\w+))|(?:(\w+)\s+squared)", goal_l)
    if m:
        n = _num(m.group(1) or m.group(2))
        if n is not None:
            checks.append({
                "id": "square_value",
                "description": f"solve() result equals {n}^2 (= {n ** 2})",
                "kind": "value_equals",
                "expected": n ** 2,
            })

    return checks


def _fmt(v: float) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def _is_goal_echo(text: str, goal_text: str | None) -> bool:
    """True when a solve() output string merely restates the task goal.

    A module that echoes the goal back has NOT computed anything — the
    goal text contains the expected number (it is where the expectation
    was derived from), so accepting it would let a module pass by
    repeating the task description. Observed concretely: the generic
    deterministic fallback module returns {'goal': <goal text>, ...},
    which substring-matched its own expectation.
    """
    if not goal_text:
        return False
    goal_norm = " ".join(goal_text.lower().split())
    text_norm = " ".join(text.lower().split())
    if goal_norm and goal_norm in text_norm:
        return True
    # Punctuation-insensitive fallback: an echo may drop or alter
    # punctuation (e.g. the generated default argument strips the
    # trailing period), so compare alphanumerics-only as well. A genuine
    # result never restates the full goal sentence, so this cannot
    # swallow real evidence.
    squashed = lambda s: "".join(ch for ch in s if ch.isalnum())  # noqa: E731
    goal_sq = squashed(goal_norm)
    return bool(goal_sq) and goal_sq in squashed(text_norm)


def _values_match(actual: Any, expected: Any, goal_text: str | None = None) -> bool:
    """Compare solve() output to the expectation, digging into results."""
    if isinstance(actual, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and actual == expected:
        return True
    if isinstance(actual, str):
        # A string that restates the goal is an echo, never evidence.
        if _is_goal_echo(actual, goal_text):
            return False
        # Accept the number embedded in text output (e.g. "720").
        return _fmt(expected) in actual
    if isinstance(actual, dict):
        for v in actual.values():
            if _values_match(v, expected, goal_text):
                return True
        return False
    if isinstance(actual, (list, tuple)):
        return any(_values_match(v, expected, goal_text) for v in actual)
    return False


def run_module_checks(
    goal: str,
    module_path: str,
    timeout: int = 20,
    override_return: Any = None,
) -> DoDCheckResult:
    """Derive checks from the goal and run them against the module's solve().

    The module is EXECUTED fresh in a subprocess (its own interpreter,
    cwd=module dir, stdin severed) and the real return value of solve()
    is captured — the adapter's own probe result is never trusted.
    """
    stem = os.path.splitext(os.path.basename(module_path))[0]
    module_dir = os.path.dirname(os.path.abspath(module_path))
    result = DoDCheckResult(module=stem)

    checks = derive_checks(goal)
    if not checks:
        result.abstained = True
        result.abstain_reason = "no executable acceptance criteria derivable from the goal"
        return result

    probe = (
        f"import json, {stem}\n"
        f"f = getattr({stem}, 'solve', None)\n"
        f"if not callable(f):\n"
        f"    print(json.dumps({{'__no_solve__': True}}))\n"
        f"else:\n"
        f"    print(json.dumps({{'__value__': f()}}))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            input=b"", capture_output=True, timeout=timeout, cwd=module_dir,
        )
    except subprocess.TimeoutExpired:
        result.execution_error = f"solve() timed out after {timeout}s"
        result.checks = [{
            "id": "execution", "description": "solve() executes",
            "passed": False, "actual": None, "error": result.execution_error,
        }]
        return result
    except OSError as exc:
        result.execution_error = f"spawn failed: {exc}"
        return result

    stdout = proc.stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        result.execution_error = (
            stderr_tail := proc.stderr.decode(errors="replace").strip()[:400]
        ) or f"exit code {proc.returncode}"
        result.checks = [{
            "id": "execution", "description": "solve() executes",
            "passed": False, "actual": None, "error": result.execution_error,
        }]
        return result

    try:
        payload = json.loads(stdout.splitlines()[-1] if stdout else "{}")
    except (json.JSONDecodeError, IndexError):
        result.execution_error = f"unparseable solve() output: {stdout[:200]!r}"
        result.checks = [{
            "id": "execution", "description": "solve() executes",
            "passed": False, "actual": None, "error": result.execution_error,
        }]
        return result

    if payload.get("__no_solve__"):
        result.execution_error = "module has no callable solve()"
        result.checks = [{
            "id": "execution", "description": "solve() executes",
            "passed": False, "actual": None, "error": result.execution_error,
        }]
        return result

    # Execution succeeded — record the REAL value.
    result.executed = True
    result.execution_output = payload.get("__value__")
    if override_return is not None:
        # Negative-test hook (tests/live drills only): act as if solve()
        # had produced this value instead. Execution still counts as clean,
        # which is exactly the "executes fine, goal not met" scenario.
        result.execution_output = override_return
    result.checks.append({
        "id": "execution", "description": "solve() executes",
        "passed": True, "actual": result.execution_output,
    })

    # Now the acceptance checks against the actual output.
    for chk in checks:
        passed = _values_match(result.execution_output, chk["expected"], goal)
        result.checks.append({
            "id": chk["id"],
            "description": chk["description"],
            "passed": passed,
            "expected": chk["expected"],
            "actual": result.execution_output,
        })

    result.satisfied = all(c["passed"] for c in result.checks)
    return result


def summarize(goal: str, module_results: list[DoDCheckResult]) -> dict[str, Any]:
    """Aggregate check results for the pipeline's DoD verification check."""
    if not module_results:
        return {
            "applicable": False,
            "reason": "no generated modules to verify",
            "satisfied": None,
            "modules": [],
        }
    abstained = [r for r in module_results if r.abstained]
    applicable = [r for r in module_results if not r.abstained]
    if not applicable:
        return {
            "applicable": False,
            "reason": abstained[0].abstain_reason if abstained else "nothing to verify",
            "satisfied": None,
            "modules": [r.to_dict() for r in module_results],
        }
    # The task's goal is satisfied when EVERY applicable module satisfies it.
    return {
        "applicable": True,
        "satisfied": all(r.satisfied for r in applicable),
        "modules": [r.to_dict() for r in module_results],
        "checks": [c for r in applicable for c in r.checks],
    }
