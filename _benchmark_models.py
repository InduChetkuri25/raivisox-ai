"""Bigger-model benchmark: qwen2.5:0.5b vs qwen2.5:3b on identical tasks.

Strictly sequential Ollama usage (no concurrent requests). Per model:
  1. text_intel understand   (LLM path, honest fallback recorded)
  2. text_intel plan         (LLM path, honest fallback recorded)
  3. codegen x2 attempts     (fresh workspace each; contract, compile,
                              solve-probe, latency, artifact recorded)
  4. DoD checks              (independent subprocess probe per artifact)

Production configuration is NOT touched: adapters are constructed fresh
after setting RAIVISOX_LLM_* in this process only. Models are unloaded
(keep_alive=0) between phases to free RAM.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, ".")

GOAL = (
    "Create a Python module that computes the sum of all integers from 1 "
    "to 100 and verify that the result is 5050."
)
MODELS = ["qwen2.5:0.5b", "qwen2.5:3b"]
OLLAMA = "http://127.0.0.1:11434"


def unload(model: str) -> None:
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate",
        data=json.dumps({"model": model, "keep_alive": 0}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        print(f"  [unloaded {model}]")
    except Exception as exc:  # noqa: BLE001
        print(f"  [unload failed: {exc}]")


ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bench_artifacts")
os.makedirs(ART_DIR, exist_ok=True)


async def bench_model(model: str) -> dict:
    os.environ["RAIVISOX_LLM_BASE_URL"] = f"http://127.0.0.1:11434/v1"
    os.environ["RAIVISOX_LLM_MODEL"] = model
    os.environ.pop("RAIVISOX_LLM_API_KEY", None)

    from integrations.adapters import AdapterContext, CodegenAdapter, TextIntelAdapter
    from services.dod.dod_checks import run_module_checks

    rec: dict = {"model": model}

    async def run_adapter(adapter, params, tag):
        t0 = time.perf_counter()
        res = await adapter.run(
            params,
            AdapterContext(actor_id="bench", task_id=model.replace(":", "_"),
                           step_id=tag, resource=f"bench:{tag}"),
        )
        return res, round(time.perf_counter() - t0, 1)

    # -- 1. analyze (requirements/intent understanding) ---------------------
    ti = TextIntelAdapter()
    res, lat = await run_adapter(ti, {"op": "analyze", "text": GOAL}, "analyze")
    o = res.output or {}
    rec["analyze"] = {
        "ok": res.success, "engine": o.get("engine"), "fallback": o.get("fallback"),
        "fallback_reason": (o.get("fallback_reason") or "")[:100],
        "latency_s": lat, "result_preview": str(o.get("result"))[:200],
    }
    print(f"  analyze: engine={o.get('engine')} fallback={o.get('fallback')} {lat}s")

    # -- 2. plan -------------------------------------------------------------
    ti2 = TextIntelAdapter()
    res, lat = await run_adapter(ti2, {"op": "plan", "text": GOAL}, "plan")
    o = res.output or {}
    rec["plan"] = {
        "ok": res.success, "engine": o.get("engine"), "fallback": o.get("fallback"),
        "fallback_reason": (o.get("fallback_reason") or "")[:100],
        "latency_s": lat, "result_preview": str(o.get("result"))[:250],
    }
    print(f"  plan: engine={o.get('engine')} fallback={o.get('fallback')} {lat}s")

    # -- 3. codegen x2 attempts (fresh workspace each) -----------------------
    attempts = []
    for i in range(2):
        ws = tempfile.mkdtemp(prefix=f"bench_{model.split(':')[1]}_{i}_")
        try:
            cg = CodegenAdapter(ws)
            res, lat = await run_adapter(cg, {"goal": GOAL}, f"codegen{i}")
            o = res.output or {}
            smoke = o.get("smoke_test") or {}
            entry = {
                "attempt": i + 1,
                "ok": res.success,
                "engine": o.get("engine"),
                "fallback": o.get("fallback"),
                "fallback_reason": (o.get("fallback_reason") or "")[:140],
                "solve_present": smoke.get("solve_present"),
                "solve_probe_exit": smoke.get("exit_code"),
                "solve_probe_stdout": str(smoke.get("stdout", ""))[:80],
                "compile_ok": o.get("compile_ok"),
                "latency_s": lat,
                "artifact": o.get("written_to"),
            }
            # -- 4. DoD (independent subprocess probe) ------------------------
            if res.success and o.get("written_to"):
                d = run_module_checks(GOAL, o["written_to"])
                entry["dod"] = {
                    "executed": d.executed,
                    "execution_output": repr(d.execution_output)[:60],
                    "satisfied": d.satisfied,
                    "failed": [c["id"] for c in d.checks if not c["passed"]],
                }
                shutil.copy(
                    o["written_to"],
                    os.path.join(ART_DIR, f"{model.replace(':', '_')}_attempt{i+1}.py"),
                )
                entry["artifact_saved"] = os.path.join(
                    ART_DIR, f"{model.replace(':', '_')}_attempt{i+1}.py"
                )
            attempts.append(entry)
            print(f"  codegen#{i+1}: engine={entry['engine']} solve={entry['solve_present']} "
                  f"dod={entry.get('dod', {}).get('satisfied')} {lat}s")
        finally:
            shutil.rmtree(ws, ignore_errors=True)
    rec["codegen_attempts"] = attempts
    rec["contract_compliance"] = f"{sum(1 for a in attempts if a['solve_present'])}/2"

    # free RAM before the next model
    unload(model)
    return rec


async def main() -> None:
    results = {"goal": GOAL, "hardware": {
        "cpu": "Intel i5-10210U 4c/8t @1.6GHz", "ram_gb": 7.8,
        "disk_free_gb": 12, "inference": "CPU-only",
    }, "models": []}
    for m in MODELS:
        print(f"=== {m} ===")
        t0 = time.perf_counter()
        r = await bench_model(m)
        r["phase_latency_s"] = round(time.perf_counter() - t0, 1)
        results["models"].append(r)
    with open("_benchmark_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print("\nSaved to _benchmark_results.json")


if __name__ == "__main__":
    asyncio.run(main())
