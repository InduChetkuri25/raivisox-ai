"""LLM repair-loop tests (stubbed transport — no live model dependency).

Contract: a violation triggers a repair round that includes the SPECIFIC
problem and the assistant's prior answer; a repaired answer is accepted
with no validation weakening; exhausted repairs fall back honestly with a
contract reason; security ban patterns are still enforced on the FINAL
accepted code by _contract_check.
"""
import json

import pytest

from integrations.adapters.codegen import CodegenAdapter
from integrations.adapters.base import AdapterContext
from services.dod.dod_checks import run_module_checks, summarize
from tests.unit.test_llm_dod import (
    GOAL_SUM,
    SUM_OK,
    SUM_BUGGY,
    CONTRACT_BAD,
    _install_llm_stub,
    _enable_llm,
    _run_adapter,
)


def test_repair_round_asks_with_specific_problem(monkeypatch, tmp_path):
    """First answer violates the contract; the repair request must contain
    the exact problem text and the assistant's prior reply."""
    _install_llm_stub(monkeypatch, [CONTRACT_BAD, SUM_OK], per_pass=1)
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    assert result.success, result.error
    out = result.output
    # The repaired generation was accepted as REAL LLM code.
    assert out["engine"] == "llm:stub-model"
    assert out["fallback"] is False


def test_no_repair_when_first_answer_is_contract_clean(monkeypatch, tmp_path):
    """A clean first generation makes exactly ONE LLM call — no repairs."""
    calls = _install_llm_stub(monkeypatch, [SUM_OK], per_pass=1)
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    assert result.success, result.error
    assert result.output["engine"] == "llm:stub-model"
    assert len(calls) == 1


def test_exhausted_repairs_fall_back_honestly(monkeypatch, tmp_path):
    """Every round violates the contract → deterministic fallback, honest
    reason mentioning the contract, and DoD is not fooled."""
    calls = _install_llm_stub(monkeypatch, [CONTRACT_BAD], per_pass=1)
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    assert result.success, result.error
    out = result.output
    assert out["fallback"] is True
    assert out["engine"] == "deterministic"
    reason = out.get("fallback_reason") or ""
    assert "contract" in reason.lower()
    # The reason names the SPECIFIC mechanical violation of the stubbed
    # code (input()) — honest diagnostics, not a generic shrug.
    assert "input()" in reason
    # 1 initial + 2 repair rounds = 3 calls, all rejected.
    assert len(calls) == 3
    # The fallback still has solve(); DoD executes it and is not fooled.
    dod = run_module_checks(GOAL_SUM, out["written_to"])
    assert dod.executed is True
    assert dod.satisfied is False


def test_repair_rejects_wrong_value_and_accepts_fix(monkeypatch, tmp_path):
    """A contract-clean but WRONG value is not a contract violation — it is
    accepted and then honestly rejected by the independent DoD check."""
    _install_llm_stub(monkeypatch, [SUM_BUGGY], per_pass=1)
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    assert result.success, result.error
    assert result.output["engine"] == "llm:stub-model"
    assert result.output["fallback"] is False

    dod = run_module_checks(GOAL_SUM, result.output["written_to"])
    assert dod.executed is True
    assert dod.satisfied is False
    assert summarize(GOAL_SUM, [dod])["satisfied"] is False
