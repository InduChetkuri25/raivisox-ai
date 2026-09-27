"""Preview API launcher: local sqlite DB + uvicorn on 127.0.0.1:8000.

Kept at the repo root and referenced by .freebuff/run.md. Forces sqlite so the
app never depends on the unreachable Postgres configured in .env.
"""
import os
import sys

os.environ["DATABASE_URL"] = (
    "sqlite+aiosqlite:///" + os.path.abspath("raivisox_live.db")
)
# Real local LLM for understand/plan/generate intelligence steps (Ollama).
# Falls back to the deterministic engines automatically when Ollama is down.
os.environ.setdefault("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
os.environ.setdefault("RAIVISOX_LLM_MODEL", "qwen2.5:0.5b")
sys.path.insert(0, ".")
sys.path.insert(0, "apps/api")

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
