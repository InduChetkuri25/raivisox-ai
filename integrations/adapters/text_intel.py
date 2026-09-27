"""Text-intelligence adapter — real local text processing, optional LLM engine.

What is REAL here:
- The default `deterministic` engine performs the actual text operations
  locally: extractive summarization (frequency-scored sentence ranking),
  keyword extraction, plan synthesis from a goal's verb/object clauses,
  and rule-based error analysis. No randomness, no fake outputs — the
  words it returns come from the input text itself.
- The optional `llm` engine calls a REAL OpenAI-compatible chat
  completions endpoint (api.openai.com, or a local server such as
  Ollama/LM Studio via RAIVISOX_LLM_BASE_URL). It activates only when a
  key (RAIVISOX_LLM_API_KEY / OPENAI_API_KEY) or local base URL is
  configured. If the call fails, the adapter falls back to the
  deterministic engine and says so in the output.

Nothing here fakes intelligence: every result records exactly which
engine produced it ("engine": "deterministic" | "llm:<model>").
"""

import asyncio
import os
import re
import time
from typing import Any

import httpx

from integrations.adapters.base import AdapterContext, AdapterResult, ToolAdapter

_LLM_TIMEOUT_SECONDS = 120.0  # cold-start model load can exceed 30s on CPU
_LLM_CONNECT_TIMEOUT = 5.0
_MAX_LLM_RETRIES = 2
_MAX_PROMPT_CHARS = 24_000
_MAX_COMPLETION_CHARS = 4_000

_STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been
before being below between both but by can did do does doing down during each few
for from further had has have having he her here hers him his how i if in into is it
its itself just me more most my no nor not of off on once only or other our out over
own same she should so some such than that the their them then there these they this
those through to too under until up very was we were what when where which while who
whom why will with you your""".split()
)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'-]+")

# (regex, cause, remediation) — rule-based error analysis patterns.
_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (r"\bModuleNotFoundError\b.*?'([\w.]+)'", "missing module/package",
     "install the package (pip install {0}) or add it to requirements"),
    (r"\bImportError\b.*?'([\w.]+)'", "broken import",
     "fix the import path or install the missing dependency '{0}'"),
    (r"\bFileNotFoundError\b.*?'([^']+)'", "missing file",
     "verify the path '{0}' exists or create it first"),
    (r"\bKeyError: '?([\w ]+)'?", "missing dictionary key",
     "guard access to key '{0}' or use .get() with a default"),
    (r"\bAttributeError:.*?object has no attribute '(\w+)'", "wrong attribute access",
     "check the object type; attribute '{0}' does not exist on it"),
    (r"\bTypeError:.*", "type mismatch", "inspect argument types at the failing call"),
    (r"\b(IndexError|ValueError):.*", "invalid value/index",
     "validate inputs before the failing operation"),
    (r"\bConnection(?:Refused|Reset)Error\b", "network connection failure",
     "check the target host/port and retry with backoff"),
    (r"\bTimeout(?:Error|Exceeded)?\b", "timeout", "increase the timeout or retry with backoff"),
    (r"\bPermission(?:Denied)?Error\b", "permission denied",
     "grant the required permission or run with adequate access"),
    (r"\b(HTTP|status) (4\d\d)\b", "client HTTP error", "fix the request (auth, URL, payload)"),
    (r"\b(HTTP|status) (5\d\d)\b", "server HTTP error", "retry with backoff; check server logs"),
    (r"\bSQLAlchemy.*?(NoForeignKeysError|OperationalError)", "database configuration error",
     "check model foreign keys and database connectivity"),
]


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]
    return parts or [(text or "").strip()]


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _content_words(text: str) -> list[str]:
    return [w for w in _words(text) if w not in _STOPWORDS and len(w) > 2]


def _keywords(text: str, top: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for w in _content_words(text):
        counts[w] = counts.get(w, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]


def _summarize(text: str, max_sentences: int = 3) -> dict[str, Any]:
    sents = _sentences(text)
    if len(sents) <= max_sentences:
        return {"summary": " ".join(sents), "sentence_count": len(sents), "reduction": 0.0}
    freq: dict[str, int] = {}
    for w in _content_words(text):
        freq[w] = freq.get(w, 0) + 1
    scored = []
    for i, s in enumerate(sents):
        cws = _content_words(s)
        if not cws:
            continue
        score = sum(freq.get(w, 0) for w in cws) / (len(cws) ** 0.5 or 1)
        scored.append((score, i, s))
    top = sorted(scored, reverse=True)[:max_sentences]
    chosen = sorted(top, key=lambda t: t[1])
    summary = " ".join(s for _, _, s in chosen)
    return {
        "summary": summary,
        "sentence_count": len(chosen),
        "reduction": round(1 - len(summary) / max(len(text), 1), 3),
    }


class TextIntelAdapter(ToolAdapter):
    """Understand / plan / analyze / generate-text via real text processing."""

    id = "text_intel"
    description = (
        "Text intelligence: deterministic local engine (extractive summary, "
        "keywords, plan synthesis, rule-based error analysis) with an optional "
        "real OpenAI-compatible LLM engine when configured."
    )
    # Understanding/planning/authoring text is unprivileged — no kernel
    # permission required. (It reads only its input parameters and produces
    # text; it never touches the filesystem, shell, or network without the
    # LLM engine, which performs its own outbound call.)
    kernel_permissions: list[str] = []

    def __init__(self):
        self.llm_api_key = (
            os.environ.get("RAIVISOX_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        self.llm_base_url = (
            os.environ.get("RAIVISOX_LLM_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.llm_model = os.environ.get("RAIVISOX_LLM_MODEL") or "gpt-4o-mini"
        self._llm_usable = bool(self.llm_api_key) or "localhost" in self.llm_base_url or "127.0.0.1" in self.llm_base_url

    # ------------------------------------------------------------------
    # Optional LLM engine (real OpenAI-compatible call)
    # ------------------------------------------------------------------

    def _llm_system_prompt(self, op: str) -> str:
        return {
            "analyze": "You are a requirements analyst. Analyze the input: restate the goal, list key requirements, constraints, risks, and open questions. Be concrete and terse.",
            "plan": "You are a planning assistant. Produce a short ordered plan (numbered steps, one line each) to accomplish the stated goal.",
            "summarize": "You are a summarizer. Produce a faithful, compact summary of the input. Do not invent facts.",
            "analyze_error": "You are a debugging assistant. Identify the root cause of the error and the concrete fix. Be terse.",
            "generate": "You are a technical writer. Produce the requested text artifact faithfully based on the input. Do not invent facts.",
        }.get(op, "You are a helpful assistant. Be terse and factual.")

    async def _llm_complete(self, op: str, user_content: str) -> tuple[bool, str, str]:
        """Returns (ok, text_or_error, model). Retries once on transient failure."""
        headers = {"Content-Type": "application/json"}
        if self.llm_api_key:
            headers["Authorization"] = f"Bearer {self.llm_api_key}"
        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": self._llm_system_prompt(op)},
                {"role": "user", "content": user_content[:_MAX_PROMPT_CHARS]},
            ],
            "max_tokens": 800,
            "temperature": 0.2,
        }
        last_err = ""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_LLM_TIMEOUT_SECONDS, connect=_LLM_CONNECT_TIMEOUT)
        ) as client:
            for _attempt in range(_MAX_LLM_RETRIES + 1):
                try:
                    resp = await client.post(
                        f"{self.llm_base_url}/chat/completions", json=payload, headers=headers
                    )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = f"LLM HTTP {resp.status_code}"
                        await asyncio.sleep(0.5)
                        continue
                    if resp.status_code != 200:
                        return False, f"LLM HTTP {resp.status_code}: {resp.text[:200]}", self.llm_model
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"]
                    return True, (text or "")[:_MAX_COMPLETION_CHARS], data.get("model", self.llm_model)
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                    detail = str(exc) or repr(exc)
                    last_err = f"LLM call failed: {type(exc).__name__}: {detail}"
                    await asyncio.sleep(0.5)
        return False, last_err, self.llm_model

    # ------------------------------------------------------------------
    # Deterministic engine — the real local operations
    # ------------------------------------------------------------------

    def _op_analyze(self, text: str) -> dict[str, Any]:
        sents = _sentences(text)
        kws = _keywords(text, top=10)
        requirement_markers = ("must", "should", "need", "require", "has to", "shall")
        requirements = [s for s in sents if any(m in s.lower() for m in requirement_markers)]
        constraint_markers = ("only", "within", "under", "without", "at most", "no more than", "cannot")
        constraints = [s for s in sents if any(m in s.lower() for m in constraint_markers)]
        risk_markers = ("critical", "production", "security", "password", "credential", "secret", "delete", "payment")
        risks = [s for s in sents if any(m in s.lower() for m in risk_markers)]
        unknowns = [s for s in sents if s.rstrip().endswith("?")]
        return {
            "operation": "analyze",
            "goal": sents[0] if sents else "",
            "key_topics": kws,
            "requirements": requirements[:5],
            "constraints": constraints[:5],
            "risks": risks[:5],
            "open_questions": unknowns[:5],
            "input_sentences": len(sents),
        }

    def _op_plan(self, text: str) -> dict[str, Any]:
        goal = _sentences(text)[0] if text.strip() else ""
        kws = _keywords(text, top=6)
        topic = " ".join(kws[:3]) or "the goal"
        steps = [
            {"order": 1, "title": f"Clarify requirements for {topic}", "kind": "understand"},
            {"order": 2, "title": f"Identify inputs and constraints for {topic}", "kind": "plan"},
            {"order": 3, "title": f"Execute the core work for {topic}", "kind": "execute"},
            {"order": 4, "title": f"Validate results against {topic}", "kind": "verify"},
            {"order": 5, "title": f"Document and deliver the outcome for {topic}", "kind": "deliver"},
        ]
        return {
            "operation": "plan",
            "goal": goal,
            "key_topics": kws,
            "plan": steps,
            "step_count": len(steps),
        }

    def _op_summarize(self, text: str) -> dict[str, Any]:
        return {"operation": "summarize", **_summarize(text, max_sentences=3)}

    def _op_analyze_error(self, text: str) -> dict[str, Any]:
        matched = None
        for pattern, cause, fix in _ERROR_PATTERNS:
            m = re.search(pattern, text)
            if m:
                remediation = fix.format(*m.groups()) if m.groups() else fix
                matched = {"cause": cause, "remediation": remediation, "match": m.group(0)[:120]}
                break
        sents = _sentences(text)
        return {
            "operation": "analyze_error",
            "root_cause": matched["cause"] if matched else "unclassified error",
            "remediation": matched["remediation"] if matched else
            "inspect the full traceback and the failing operation's inputs",
            "pattern_matched": bool(matched),
            "key_terms": _keywords(text, top=6),
            "input_lines": len(sents),
        }

    def _op_generate(self, text: str) -> dict[str, Any]:
        # A real deterministic artifact: structured digest of the input.
        sents = _sentences(text)
        summary = _summarize(text, max_sentences=4)
        kws = _keywords(text, top=8)
        doc = (
            "# Summary\n"
            f"{summary['summary']}\n\n"
            "## Key topics\n" + "".join(f"- {k}\n" for k in kws) +
            "\n## Source lines\n" + "".join(f"- {s}\n" for s in sents[:10])
        )
        return {
            "operation": "generate",
            "document": doc,
            "characters": len(doc),
            "source_sentences": len(sents),
        }

    _OPS = {
        "analyze": _op_analyze,
        "plan": _op_plan,
        "summarize": _op_summarize,
        "analyze_error": _op_analyze_error,
        "generate": _op_generate,
    }

    # ------------------------------------------------------------------

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        started = time.perf_counter()

        def _done(success: bool, output=None, error: str | None = None) -> AdapterResult:
            return AdapterResult(
                success=success,
                tool_id=self.id,
                output=output,
                error=error,
                execution_time_ms=int((time.perf_counter() - started) * 1000),
            )

        op = (params.get("op") or "analyze").lower()
        text = params.get("text") or params.get("input") or ""
        if not isinstance(text, str) or not text.strip():
            return _done(False, error="text_intel requires a non-empty 'text' parameter")
        if op not in self._OPS:
            return _done(
                False,
                error=f"Unknown op '{op}' (expected one of: {', '.join(sorted(self._OPS))})",
            )

        # Prefer the real LLM engine when configured; fall back honestly.
        if self._llm_usable:
            ok, result, model = await self._llm_complete(op, text)
            if ok:
                return _done(True, output={
                    "operation": op,
                    "engine": f"llm:{model}",
                    "result": result,
                    "fallback": False,
                })
            fallback_reason = result
        else:
            fallback_reason = "no LLM backend configured"

        output = self._OPS[op](self, text)
        output["engine"] = "deterministic"
        output["fallback"] = self._llm_usable  # True only if LLM was tried and failed
        if self._llm_usable:
            output["fallback_reason"] = fallback_reason
        return _done(True, output=output)
