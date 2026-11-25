# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Modular evidence-ledger, on-demand MCP, and export-auditing coordinator for PLE."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import io
import json
import os
import posixpath
import re
import types
from typing import Any
import zipfile

import openai
import taskbridge

from evidence import EvidenceLedger, convert_unit, normalize_unit
from mcp_catalog import MCPCatalog, estimate_schema_tokens, tool_schema
from pricing import UsageTracker
from verifiers import (
    audit_decision_memo,
    audit_ifc4_bytes,
    audit_workbook_bytes,
    audit_zip_deliverables,
)

MAX_EFFORT_MODELS = ("claude", "glm", "gpt-6", "deepseek-v4")
RESPONSES_MODELS = ("gpt-6",)
RESPONSES_ONLY = re.compile(
    r"/v1/responses|not supported.{0,80} in /v1/chat/completions"
)
CONTEXT_FULL = re.compile(
    r"context.?(length|window|size|limit)|maximum context|too many"
    r" tokens|prompt is too long|maximum prompt length"
    r"|(input|prompt|request|messages?)\b[^.]{0,40}(too long|exceed)"
    r"|exceed[^.]{0,40}(token|context)",
    re.I,
)
WINDOW_RE = re.compile(
    r"maximum context length (?:is |of )?(\d+)"
    r"|context (?:window|length|size)(?: limit)?(?: is| of)? (\d+)"
    r"|tokens? > (\d+) maximum|maximum number of tokens allowed \((\d+)\)"
    r"|maximum prompt length is (\d+)",
    re.I,
)
PROMPT_RE = re.compile(
    r"(\d+) input tokens|(\d+) in the messages|prompt is too long: (\d+)"
    r"|input token count \((\d+)\)|request contains (\d+) tokens",
    re.I,
)
UNSUPPORTED = re.compile(
    r"unsupported|unrecogni[sz]ed|unknown|not supported|does not support|not"
    r" (allowed|permitted)|extra (inputs|fields)|unexpected",
    re.I,
)

CALL_FIELDS = {"index", "id", "type", "function"}
FUNCTION_FIELDS = {"name", "arguments"}
DELTA_FIELDS = {
    "role",
    "content",
    "tool_calls",
    "function_call",
    "refusal",
    "audio",
    "annotations",
    "reasoning_content",
    "reasoning",
}
BAD_REQUEST = frozenset({
    "invalid_request_error",
    "context_length_exceeded",
    "invalid_value",
    "invalid_prompt",
})

TOOL_RESULT_CHARS = 30_000
KEEP_RECENT_RESULTS = 12
MIN_OUTPUT = 1024
UNREAD_KEEP = 600
CONTEXT_MARGIN = 1024
RETRY_WAITS = (5, 15, 30, 60, 120)
MAX_INCUMBENT_BYTES = 20 * 1024 * 1024

AUDIT_EXTENSIONS = (".xlsx", ".xlsm", ".zip", ".ifc", ".docx")
OUTPUT_PATH_RE = re.compile(
    r"(?:^|[\s\"'(=])(/[^\s\"')<>]+\.(?:xlsx|xlsm|zip|ifc|docx|md|txt|json|pdf|csv))",
    re.I,
)

SYSTEM_PROMPT = """You are an expert physical-engineering coordinator working inside a Linux task container.
Tools act in {workdir} (and no external internet exists).{mcp_note}

Follow a disciplined 5-step engineering workflow:
1. INDEX & PARSE REQUIREMENTS: List project files/revisions first. Record exact requested output paths, formats, units, dates, tolerances, standards, and any required MCP actions.
2. RECORD GOVERNING EVIDENCE: Use record_evidence for consequential values (original value/unit, source locator, revision, dependents). Resolve conflicts explicitly with `supersedes` and check units with `convert_units`.
3. COMPUTE & DECIDE DETERMINISTICALLY: Write clean Python verification/generation scripts executed via `bash`; check unit conversions, balances, geometry/clearances, and schedules numerically rather than by hand.
4. PRODUCE REAL DELIVERABLES EARLY: Write exact requested formats (XLSX, DOCX, IFC4 STEP, PDF, Markdown) at exact requested paths and perform required task-local MCP actions with read-back verification. Use save_incumbent once a deliverable passes audit.
5. COLD-READ VERIFY BEFORE FINISHING: Run `run_export_audit` (or Python readback checks) on saved bytes to verify workbook formulas/rollups/narrative reconciliation, IFC clashes/connectivity, or decision-memo prerequisite consistency.

Time budget: about {minutes} minutes remaining (live updates follow tool results). When all requested artifacts and verifications are verified complete, answer without tool calls summarizing exact deliverable paths and traced results."""

SUMMARY_PROMPT = (
    "The conversation is getting long, so earlier turns will be removed. Summarize precisely: "
    "task requirements, file locators and revisions found, recorded evidence values/units, "
    "every file created or modified (exact path and verification status), and what remains."
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RuntimeConfig:
    """Configuration parsed from container environment variables."""

    model_name: str
    reasoning_effort: str
    cache_mark: bool
    responses_api: bool
    max_output_tokens: int
    compact_at_tokens: int
    reserve_verify_sec: int
    budget_cap_usd: float
    mcp_discovery_mode: str
    enable_evidence_ledger: bool
    enable_export_verifier: bool
    enable_incumbent_retention: bool
    enable_image_input: bool
    max_verify_repairs: int

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        model = os.environ.get("MODEL_NAME", "google-cloud/gemini-4-neon")
        effort = os.environ.get("REASONING_EFFORT") or (
            "max" if any(m in model.lower() for m in MAX_EFFORT_MODELS) else "high"
        )
        return cls(
            model_name=model,
            reasoning_effort=effort,
            cache_mark="claude" in model.lower(),
            responses_api=model.lower().startswith(RESPONSES_MODELS),
            max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", "32000")),
            compact_at_tokens=int(os.environ.get("COMPACT_AT_TOKENS", "140000")),
            reserve_verify_sec=int(os.environ.get("RESERVE_VERIFY_SEC", "150")),
            budget_cap_usd=float(os.environ.get("BUDGET_CAP_USD", "0") or "0"),
            mcp_discovery_mode=os.environ.get("MCP_DISCOVERY_MODE", "ondemand").strip().lower(),
            enable_evidence_ledger=_env_bool("ENABLE_EVIDENCE_LEDGER", True),
            enable_export_verifier=_env_bool("ENABLE_EXPORT_VERIFIER", True),
            enable_incumbent_retention=_env_bool("ENABLE_INCUMBENT_RETENTION", True),
            enable_image_input=_env_bool("ENABLE_IMAGE_INPUT", False),
            max_verify_repairs=max(0, int(os.environ.get("MAX_VERIFY_REPAIRS", "2"))),
        )


def build_base_tools(config: RuntimeConfig) -> list[dict[str, Any]]:
    """Constructs coordinator tool definitions matching enabled workflow capabilities."""
    tools = [
        tool_schema(
            "bash",
            "Execute a shell command in the task container (starts in workdir).",
            ["command"],
            command={"type": "string"},
            timeout_sec={"type": "integer", "description": "Timeout in seconds (default 120)."},
        ),
        tool_schema(
            "read_file",
            "Read a UTF-8 text file from the task container.",
            ["path"],
            path={"type": "string"},
        ),
        tool_schema(
            "write_file",
            "Create or overwrite a text file in the task container; parent directories are created.",
            ["path", "content"],
            path={"type": "string"},
            content={"type": "string"},
            append={"type": "boolean", "description": "Append instead of overwriting."},
        ),
        tool_schema(
            "edit_file",
            "Replace exact text in a file inside the task container.",
            ["path", "old_text", "new_text"],
            path={"type": "string"},
            old_text={"type": "string"},
            new_text={"type": "string"},
            replace_all={"type": "boolean"},
        ),
    ]
    if config.enable_evidence_ledger:
        tools.extend([
            tool_schema(
                "record_evidence",
                "Record a governing physical value with unit, source locator, revision, and dependent keys.",
                ["key", "value", "unit", "source_locator"],
                key={"type": "string"},
                value={"type": ["string", "number", "boolean", "object", "array"]},
                unit={"type": "string"},
                source_locator={"type": "string"},
                revision={"type": "string"},
                dependents={"type": "array", "items": {"type": "string"}},
                supersedes={"type": "string"},
                notes={"type": "string"},
            ),
            tool_schema(
                "query_evidence",
                "Query recorded evidence entries and unresolved source conflicts.",
                [],
                key={"type": "string", "description": "Exact key, or omit for full summary."},
            ),
            tool_schema(
                "convert_units",
                "Deterministically convert a numeric scalar between compatible physical units.",
                ["value", "from_unit", "to_unit"],
                value={"type": "number"},
                from_unit={"type": "string"},
                to_unit={"type": "string"},
            ),
        ])
    if config.enable_export_verifier:
        tools.append(
            tool_schema(
                "run_export_audit",
                "Open exported deliverable bytes (XLSX, ZIP, IFC4 STEP, DOCX/MD) and run deterministic audit checks.",
                ["path"],
                path={"type": "string", "description": "Container path to deliverable."},
                audit_kind={"type": "string", "enum": ["auto", "workbook", "zip", "ifc", "decision_memo"]},
                narrative_path={"type": "string", "description": "Optional companion narrative markdown/text path."},
                required_items={"type": "array", "items": {"type": "string"}},
                required_endpoints={"type": "object", "description": "Mapping {service: [[x,y,z], ...]}."},
                source_points={"type": "object", "description": "Mapping {service: [x,y,z]}."},
                forbidden_zones={"type": "array", "items": {"type": "object"}},
                requested_date={"type": "string"},
                prerequisites={"type": "array", "items": {"type": "object"}},
                expected_work_package={"type": "object"},
                actual_work_package={"type": "object"},
                require_cached_formulas={"type": "boolean"},
            )
        )
    if config.enable_incumbent_retention:
        tools.extend([
            tool_schema(
                "save_incumbent",
                "Snapshot a verified deliverable in memory so later failed edits can be rolled back.",
                ["path"],
                path={"type": "string"},
                note={"type": "string"},
            ),
            tool_schema(
                "restore_incumbent",
                "Restore the last saved incumbent bytes for `path` into the task container.",
                ["path"],
                path={"type": "string"},
            ),
        ])
    if config.enable_image_input:
        tools.append(
            tool_schema(
                "read_image",
                "Read an image crop (.png/.jpg/.webp) from the container for visual inspection.",
                ["path"],
                path={"type": "string"},
            )
        )
    return tools


def create_client() -> openai.AsyncOpenAI:
    """Creates an AsyncOpenAI client pointing at container MODEL_BASE_URL."""
    return openai.AsyncOpenAI(
        base_url=os.environ.get("MODEL_BASE_URL"),
        api_key=os.environ.get("MODEL_API_KEY") or "none",
        max_retries=0,
        timeout=900,
    )


def clip(text: str, limit: int = TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n[... {len(text) - limit} characters cut ...]\n{text[-(limit - head):]}"


def parse_arguments(call: dict[str, Any]) -> dict[str, Any] | str:
    def reject(constant: str) -> None:
        raise ValueError(f"{constant} is not valid JSON")

    try:
        args = json.loads(call["arguments"] or "{}", parse_constant=reject)
        if not isinstance(args, dict):
            raise ValueError("the arguments must be a JSON object")
        call["arguments"] = json.dumps(args, ensure_ascii=False).encode("utf-8", "replace").decode("utf-8")
        return args
    except ValueError as exc:
        call["arguments"] = "{}"
        return f"error: the arguments are not a valid JSON object ({exc}); send the call again"


def visible(text: str) -> str:
    return re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S).strip()


def extra_fields(part: Any, known: set[str]) -> dict[str, Any]:
    fields = getattr(part, "model_extra", None) or {}
    return {k: v for k, v in fields.items() if k not in known and v is not None}


def thinking_blocks(blocks: list[dict[str, Any]], new: list[Any]) -> list[dict[str, Any]]:
    out = [dict(b) for b in blocks]
    for b in new:
        if not isinstance(b, dict):
            continue
        last = out[-1] if out else None
        if (
            last is None
            or last.get("signature")
            or last.get("data")
            or b.get("data")
            or last.get("type") != b.get("type")
        ):
            out.append(dict(b))
            continue
        last["thinking"] = (last.get("thinking") or "") + (b.get("thinking") or "")
        if b.get("signature"):
            last["signature"] = b["signature"]
    return out


def merged(old: Any, new: Any, key: str = "") -> Any:
    if key == "thinking_blocks" and isinstance(new, list):
        return thinking_blocks(old if isinstance(old, list) else [], new)
    if isinstance(old, dict) and isinstance(new, dict):
        return {**old, **{k: merged(old.get(k), v, k) for k, v in new.items()}}
    if isinstance(old, list) and isinstance(new, list):
        return old + new
    return new


def signed(block: Any) -> bool:
    return isinstance(block, dict) and bool(block.get("signature") or block.get("data"))


def drop_thinking(message: dict[str, Any], unless: Any = lambda _: False) -> dict[str, Any]:
    kept = [b for b in message.get("thinking_blocks") or [] if unless(b)]
    if kept:
        message["thinking_blocks"] = kept
    else:
        message.pop("thinking_blocks", None)
    return message


def marked(messages: list[dict[str, Any]], enabled: bool) -> list[dict[str, Any]]:
    if not enabled or not messages:
        return messages
    out = list(messages)
    for i in {0, len(out) - 1}:
        m, content = out[i], out[i].get("content")
        if isinstance(content, str) and content:
            out[i] = {
                **m,
                "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}],
            }
        else:
            out[i] = {**m, "cache_control": {"type": "ephemeral"}}
    return out


def response_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
        elif m["role"] != "assistant":
            items.append({"role": m["role"], "content": m["content"]})
        elif "output_items" in m:
            calls = iter(m.get("tool_calls") or [])
            for item in m["output_items"]:
                call = next(calls, None) if item.get("type") == "function_call" else None
                if call:
                    item = {
                        **item,
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                items.append(item)
        else:
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            items.extend(
                {
                    "type": "function_call",
                    "call_id": c["id"],
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"],
                }
                for c in m.get("tool_calls") or []
            )
    return items


def chat_usage(usage: Any) -> Any:
    if usage is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    return types.SimpleNamespace(
        prompt_tokens=usage.input_tokens or 0,
        completion_tokens=usage.output_tokens or 0,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=(getattr(details, "cached_tokens", 0) or 0)),
    )


class Reply:
    """Accumulates streamed deltas from one Chat Completions or Responses call."""

    def __init__(self) -> None:
        self.text = ""
        self.reasoning = ""
        self.calls: dict[int, dict[str, Any]] = {}
        self.usage: Any = None
        self.finish: str | None = None
        self.extra: dict[str, Any] = {}
        self._by_index: dict[Any, int] = {}
        self._by_id: dict[str, int] = {}
        self._last: int | None = None

    def add_call(self, part: Any) -> None:
        index = getattr(part, "index", None)
        call_id = getattr(part, "id", None) or ""
        function = getattr(part, "function", None)
        name = (getattr(function, "name", None) or "") if function else ""
        key = self._by_index.get(index) if index is not None else None
        if key is not None and call_id and self.calls[key]["id"] not in ("", call_id):
            key = None
        if key is not None and not call_id and name and self._another_call(self.calls[key], name):
            key = None
        if key is None and call_id:
            key = self._by_id.get(call_id)
        if key is None and index is None and not call_id and self._last is not None:
            if not (name and self._another_call(self.calls[self._last], name)):
                key = self._last
        if key is None:
            key = len(self.calls)
            self.calls[key] = {"id": "", "name": "", "arguments": "", "extra": {}, "function_extra": {}}
        if index is not None:
            self._by_index[index] = key
        if call_id:
            self._by_id[call_id] = key
        call = self.calls[key]
        call["id"] = call["id"] or call_id
        call["name"] = call["name"] or name
        call["arguments"] += (getattr(function, "arguments", None) or "") if function else ""
        call["extra"] = merged(call["extra"], extra_fields(part, CALL_FIELDS))
        if function:
            call["function_extra"] = merged(call["function_extra"], extra_fields(function, FUNCTION_FIELDS))
        self._last = key

    @staticmethod
    def _another_call(call: dict[str, Any], name: str) -> bool:
        if not call["name"]:
            return False
        if name != call["name"]:
            return True
        try:
            return isinstance(json.loads(call["arguments"]), dict)
        except ValueError:
            return False

    def message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {**self.extra, "role": "assistant", "content": self.text or None}
        if self.calls:
            msg["tool_calls"] = [
                {
                    **c["extra"],
                    "id": c["id"],
                    "type": "function",
                    "function": {**c["function_extra"], "name": c["name"], "arguments": c["arguments"]},
                }
                for c in self.calls.values()
            ]
        if self.reasoning:
            msg["reasoning_content"] = self.reasoning
        return drop_thinking(msg, unless=signed)


class OutOfTime(Exception):
    """Raised when retrying would exceed the reserved wall-clock margin."""


class CutOff(asyncio.TimeoutError):
    """Raised when a stream hits the reserved deadline cutoff."""


class BudgetExhausted(Exception):
    """Raised when BUDGET_CAP_USD has been reached before completing the turn."""


class StreamBadRequest(openai.OpenAIError):
    """Raised for non-retryable 400-equivalent in-stream errors."""


class StreamCut(openai.OpenAIError):
    """Raised when an SSE stream drops before emitting a finish reason."""


def stream_error(error: Any) -> openai.OpenAIError:
    def field(name: str) -> Any:
        return error.get(name) if isinstance(error, dict) else getattr(error, name, None)

    message = str(field("message") or "")
    named = {field("type"), field("code")} & BAD_REQUEST
    if named or CONTEXT_FULL.search(message):
        return StreamBadRequest(message)
    return StreamCut(f"the stream reported an error: {message}")


class EngineeringCoordinator:
    """Single-coordinator tool-calling agent with MCP catalog, ledger, verifiers, and budget control."""

    def __init__(
        self,
        session: taskbridge.Session,
        config: RuntimeConfig | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        self.session = session
        self.config = config or RuntimeConfig.from_env()
        self.client = client or create_client()
        self.tools = build_base_tools(self.config)
        self.catalog = MCPCatalog(mode=self.config.mcp_discovery_mode)
        self.ledger = EvidenceLedger()
        self.usage_tracker = UsageTracker(
            self.config.model_name,
            budget_cap_usd=self.config.budget_cap_usd,
        )
        self.messages: list[dict[str, Any]] = []
        self.prompt_tokens = 0
        self.output_ceiling = self.config.max_output_tokens
        self.context_window: int | None = None
        self.max_output = self.output_ceiling
        self.token_limit = "max_completion_tokens"
        self.effort: dict[str, Any] = {"reasoning_effort": self.config.reasoning_effort}
        self.responses = self.config.responses_api
        self.finish_margin = float(self.config.reserve_verify_sec)
        self.touched_deliverables: dict[str, int] = {}
        self.bash_detected_paths: set[str] = set()
        self.verified_paths: dict[str, int] = {}
        self.incumbents: dict[str, tuple[bytes, str]] = {}
        self.incumbent_total_bytes = 0
        self._write_seq = 0
        self._pending_images: list[tuple[str, str]] = []
        self._last_durable_summary = ""

    async def run(self) -> str:
        """Runs the coordinator loop until deliverable verification passes or stops cleanly."""
        mcp_note = await self.catalog.connect(self.session)
        self.tools = build_base_tools(self.config) + self.catalog.exposed_tools()
        left = self.session.time_left()
        self.finish_margin = (
            float(self.config.reserve_verify_sec)
            if left is None
            else min(float(self.config.reserve_verify_sec), max(left, 0.0) / 4.0)
        )
        self.messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(
                    workdir=self.session.workdir,
                    mcp_note=mcp_note,
                    minutes=int(left / 60) if left else "unlimited",
                ),
            },
            {"role": "user", "content": self.session.instruction},
        ]
        nudges, head, repairs = 0, "", 0
        try:
            while True:
                left = self.session.time_left()
                if left is not None and left < self.finish_margin:
                    return await self.finish("Time is almost up.")
                if self.usage_tracker.budget_exhausted():
                    return self._budget_stop_summary()
                try:
                    reply = await self.complete(head=head)
                except BudgetExhausted:
                    return self._budget_stop_summary()
                except OutOfTime:
                    return await self.finish("Time is almost up, and the model is not answering.")
                except CutOff:
                    return await self.finish("Time is almost up.")

                arguments = [parse_arguments(c) for c in reply.calls.values()]
                if reply.text or reply.calls:
                    self.messages.append(reply.message())
                text = visible(head + reply.text)
                if text:
                    self._last_durable_summary = text
                    self.session.final_message = text

                if not reply.calls:
                    if visible(reply.text) and reply.finish == "length":
                        nudges, head = 0, head + reply.text
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"Your reply hit the {self.max_output}-token output limit. Continue where it stopped "
                                "and write long content to files via write_file."
                            ),
                        })
                        continue
                    if visible(reply.text):
                        if repairs < self.config.max_verify_repairs and not self.usage_tracker.budget_exhausted():
                            gate_feedback = await self._pre_completion_gate(text)
                            if gate_feedback:
                                repairs += 1
                                nudges, head = 0, ""
                                self.messages.append({"role": "user", "content": gate_feedback})
                                continue
                        return text or self._last_durable_summary or self.session.final_message
                    if nudges >= 2:
                        return text or self._last_durable_summary or self.session.final_message
                    nudges += 1
                    self.messages.append({
                        "role": "user",
                        "content": "Continue verification or finish with your verified summary and deliverable paths.",
                    })
                    continue

                nudges, head = 0, ""
                for call, args in zip(reply.calls.values(), arguments):
                    result = (
                        await self.run_tool(call["name"], args)
                        if isinstance(args, dict)
                        else args
                    )
                    if reply.finish == "length" and isinstance(args, str):
                        result += f" Reply hit {self.max_output}-token limit; write large files in parts."
                    self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

                if self._pending_images:
                    content_parts: list[dict[str, Any]] = [
                        {"type": "text", "text": f"Loaded image crop: {img_path}"}
                        for img_path, _ in self._pending_images
                    ]
                    for _, data_url in self._pending_images:
                        content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    self.messages.append({"role": "user", "content": content_parts})
                    self._pending_images.clear()

                left = self.session.time_left()
                if left is not None:
                    self.messages[-1]["content"] = (
                        f"{self.messages[-1]['content']}\n[{int(left // 60)} minutes left]"
                        if isinstance(self.messages[-1]["content"], str)
                        else self.messages[-1]["content"]
                    )
                if self.prompt_tokens > self.compact_at():
                    await self.compact()
        except asyncio.CancelledError:
            self.session.final_message = self._last_durable_summary or self.session.final_message
            raise

    def _budget_stop_summary(self) -> str:
        s = self.usage_tracker.summary()
        note = (
            f"\n[Budget cap ${s['budget_cap_usd']:.2f} reached after "
            f"${s['total_cost_usd']:.4f} over {s['calls']} model call(s).]"
        )
        base = self._last_durable_summary or self.session.final_message or "Stopped at configured budget cap."
        final = base if "[Budget cap" in base else base + note
        self.session.final_message = final
        return final

    async def _pre_completion_gate(self, narrative_text: str) -> str | None:
        """Runs deterministic cold-read checks before accepting final completion."""
        issues: list[str] = []
        if self.config.enable_evidence_ledger:
            conflicts = self.ledger.unresolved_conflicts()
            if conflicts:
                issues.append(f"Unresolved EvidenceLedger conflicts on keys: {', '.join(sorted(conflicts))}.")
        if self.config.enable_export_verifier:
            for path, write_seq in sorted(self.touched_deliverables.items()):
                lower = path.lower()
                if not lower.endswith(AUDIT_EXTENSIONS):
                    continue
                if self.verified_paths.get(path, -1) >= write_seq:
                    continue
                audit_res = await self._run_audit_internal(path, narrative_text=narrative_text)
                if path in self.bash_detected_paths and str(audit_res.get("error", "")).startswith("read failed:"):
                    continue
                if audit_res.get("ok"):
                    self.verified_paths[path] = write_seq
                    await self._snapshot_incumbent(path, "auto cold-read pass")
                else:
                    defects = audit_res.get("defects") or []
                    detail = "; ".join(
                        f"{d.get('item')}: {d.get('earliest_suspect_dependency')}" for d in defects[:4]
                    )
                    issues.append(f"Cold-read audit failed for {path}: {detail or audit_res.get('error')}")
        if not issues:
            return None
        return (
            "Before finalizing, resolve these concrete verification issues (fix the underlying file/data or resolve "
            "conflicts via record_evidence/restore_incumbent, then re-verify with run_export_audit):\n- "
            + "\n- ".join(issues)
        )

    async def finish(self, why: str) -> str:
        """Requests a no-tool wrap-up summary within remaining time/budget."""
        if self.usage_tracker.budget_exhausted():
            return self._budget_stop_summary()
        self.messages.append({
            "role": "user",
            "content": (
                f"{why} Stop tool calls and return a concise verified summary of deliverables, "
                "paths, and key checked results."
            ),
        })
        left = self.session.time_left()
        reserve = 0.0 if left is None else left - max(5, taskbridge.time_share(left, 30))
        try:
            reply = await self.complete(tool_choice="none", reserve=reserve, head="")
            text = visible(reply.text)
        except (CutOff, openai.OpenAIError, OutOfTime, BudgetExhausted):
            text = ""
        return text or self._last_durable_summary or self.session.final_message

    async def complete(
        self,
        tool_choice: str = "auto",
        reserve: float | None = None,
        head: str | None = None,
    ) -> Reply:
        if self.usage_tracker.budget_exhausted():
            raise BudgetExhausted("Configured budget cap reached")
        reserve_sec = self.finish_margin if reserve is None else reserve
        retry_scale = float(os.environ.get("RETRY_BASE_SEC", "5")) / 5.0
        waits = iter(w * retry_scale for w in RETRY_WAITS)
        while True:
            try:
                reply = await self.stream(self.messages, tool_choice, reserve_sec, head)
                if reply.finish is None:
                    raise StreamCut("stream ended without a finish reason")
                await self.record(reply)
                return reply
            except openai.APIStatusError as exc:
                if exc.status_code == 400 and await self.remedy(str(exc)):
                    continue
                if not (exc.status_code in (408, 409, 429) or exc.status_code >= 500):
                    raise
                failure: Exception = exc
            except StreamBadRequest as exc:
                if await self.remedy(str(exc)):
                    continue
                raise
            except (openai.APIError, StreamCut) as exc:
                failure = exc
            wait = next(waits, None)
            if wait is None:
                raise failure
            left = self.session.time_left()
            if left is not None and left - wait < reserve_sec:
                await self.session.log(f"model call failed ({str(failure)[:300]}); no time to wait {wait}s")
                raise OutOfTime(str(failure)[:300]) from failure
            await self.session.log(f"model call failed ({str(failure)[:300]}); retry in {wait}s")
            await asyncio.sleep(wait)

    async def remedy(self, error: str) -> bool:
        if CONTEXT_FULL.search(error):
            sent, measured = self.max_output, self.learn_context(error)
            if measured and (self.room() or 0) >= MIN_OUTPUT and self.output_limit() < sent:
                return True
            return bool(
                await self.compact(force=True)
                or (not measured and self.output_limit() < sent)
                or self.cut_unread()
            )
        return (
            self.use_responses(error)
            or self.use_max_tokens(error)
            or self.drop_effort(error)
            or self.shrink_output(error)
        )

    def use_responses(self, error: str) -> bool:
        if self.responses or not RESPONSES_ONLY.search(error):
            return False
        self.responses = True
        return True

    def use_max_tokens(self, error: str) -> bool:
        if (
            self.token_limit != "max_completion_tokens"
            or "max_completion_tokens" not in error
            or not UNSUPPORTED.search(error)
        ):
            return False
        self.token_limit = "max_tokens"
        return True

    def drop_effort(self, error: str) -> bool:
        if not self.effort or not re.search(r"reasoning[._]effort", error):
            return False
        self.effort = {}
        return True

    def shrink_output(self, error: str) -> bool:
        if UNSUPPORTED.search(error):
            return False
        limit = re.search(r"(\d+)\s*\(exclusive\)", error)
        stated = re.search(
            r"at most (\d+)|(\d+), which is the maximum|maximum (?:allowed )?"
            r"(?:number of )?(?:output|completion) tokens (?:is |of )?(\d+)",
            error,
            re.I,
        )
        if limit:
            new = int(limit.group(1)) - 1
        elif stated:
            new = int(next(g for g in stated.groups() if g))
        elif re.search(r"max_?(completion_)?tokens|maxOutputTokens|output tokens", error, re.I):
            new = self.max_output // 2
        else:
            return False
        if new >= self.max_output or new < MIN_OUTPUT:
            return False
        self.output_ceiling = new
        return True

    def learn_context(self, error: str) -> bool:
        window, prompt = WINDOW_RE.search(error), PROMPT_RE.search(error)
        if window:
            self.context_window = int(next(g for g in window.groups() if g))
        if prompt:
            self.prompt_tokens = int(next(g for g in prompt.groups() if g))
        return bool(prompt and self.context_window)

    def room(self) -> int | None:
        return (self.context_window - self.prompt_tokens - CONTEXT_MARGIN) if self.context_window else None

    def output_limit(self) -> int:
        room = self.room()
        return self.output_ceiling if room is None else max(MIN_OUTPUT, min(self.output_ceiling, room))

    def compact_at(self) -> int:
        if not self.context_window:
            return self.config.compact_at_tokens
        room = min(self.output_ceiling, self.context_window // 2)
        return max(
            self.context_window // 4,
            min(self.config.compact_at_tokens, self.context_window - room - CONTEXT_MARGIN),
        )

    def estimate_prompt_tokens(self) -> int:
        return sum(
            len(json.dumps({k: v for k, v in m.items() if k != "output_items"}))
            for m in self.messages
        ) // 4

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_choice: str,
        reserve: float,
        head: str | None = None,
    ) -> Reply:
        left = self.session.time_left()
        self.max_output = self.output_limit()
        reply = Reply()

        async def read() -> None:
            if self.responses:
                await self.respond(reply, messages, tool_choice)
                return
            stream = await self.client.chat.completions.create(
                model=self.config.model_name,
                messages=marked(messages, self.config.cache_mark),
                tools=self.tools,
                tool_choice=tool_choice,
                stream=True,
                stream_options={"include_usage": True},
                **self.effort,
                **{self.token_limit: self.max_output},
            )
            async for chunk in stream:
                reply.usage = chunk.usage or reply.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reply.finish = chunk.choices[0].finish_reason or reply.finish
                reply.text += delta.content or ""
                reply.reasoning += (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                    or ""
                )
                reply.extra = merged(reply.extra, extra_fields(delta, DELTA_FIELDS))
                for part in delta.tool_calls or []:
                    reply.add_call(part)

        try:
            await asyncio.wait_for(read(), None if left is None else max(0.5, left - reserve))
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if head is not None:
                self.session.final_message = visible(head + reply.text) or self.session.final_message
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise CutOff() from None
        except openai.APIError as exc:
            said = {
                "message": getattr(exc, "message", None) or str(exc),
                "type": getattr(exc, "type", None),
                "code": getattr(exc, "code", None),
            }
            if isinstance(exc, openai.APIStatusError) or isinstance(stream_error(said), StreamCut):
                raise
            raise StreamBadRequest(said["message"]) from exc

        for n, call in reply.calls.items():
            call["id"] = call["id"] or f"call_{n}_{len(self.messages)}"
            call["name"] = call["name"].split("<|")[0].strip()
        return reply

    async def respond(self, reply: Reply, messages: list[dict[str, Any]], tool_choice: str) -> None:
        effort = self.effort.get("reasoning_effort")
        stream = await self.client.responses.create(
            model=self.config.model_name,
            input=response_input(messages),
            tools=[{"type": "function", **t["function"], "strict": False} for t in self.tools],
            tool_choice=tool_choice,
            stream=True,
            store=False,
            include=["reasoning.encrypted_content"],
            max_output_tokens=self.max_output,
            **({"reasoning": {"effort": effort}} if effort else {}),
        )
        async for event in stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                reply.text += event.delta or ""
            elif kind in ("error", "response.failed"):
                raise stream_error(getattr(getattr(event, "response", None), "error", None) or event)
            elif kind in ("response.completed", "response.incomplete"):
                done = event.response
                items = [i.model_dump(exclude_none=True) for i in done.output or []]
                reply.extra = {"output_items": items}
                reply.text = "".join(
                    part.get("text", "")
                    for item in items
                    if item.get("type") == "message"
                    for part in item.get("content") or []
                    if part.get("type") == "output_text"
                )
                for item in items:
                    if item.get("type") == "function_call":
                        reply.calls[len(reply.calls)] = {
                            "id": item.get("call_id") or "",
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "",
                            "extra": {},
                            "function_extra": {},
                        }
                reply.usage = chat_usage(done.usage)
                reason = getattr(done.incomplete_details, "reason", None)
                if kind == "response.completed":
                    reply.finish = "tool_calls" if reply.calls else "stop"
                else:
                    reply.finish = reason if reason == "content_filter" else "length"

    async def record(self, reply: Reply) -> None:
        schema_toks = estimate_schema_tokens(self.tools)
        if reply.usage is None:
            est = self.estimate_prompt_tokens()
            self.prompt_tokens = est
            self.usage_tracker.record(est, len(reply.text) // 4, 0, reconstructed=True, schema_tokens=schema_toks)
            return
        in_tok = int(reply.usage.prompt_tokens or 0)
        out_tok = int(reply.usage.completion_tokens or 0)
        cached = int(getattr(getattr(reply.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
        self.prompt_tokens = in_tok
        self.usage_tracker.record(in_tok, out_tok, cached, reconstructed=False, schema_tokens=schema_toks)
        await self.session.usage(self.config.model_name, in_tok, out_tok, cached)

    async def run_tool(self, name: str, args: dict[str, Any]) -> str:
        await self.session.log(f"{name} {json.dumps(args)[:400]}")
        try:
            if self.catalog.is_mcp_tool(name):
                return clip(await self.catalog.execute_tool(name, args, max_chars=TOOL_RESULT_CHARS))
            handlers = {
                "bash": self._tool_bash,
                "read_file": self._tool_read_file,
                "write_file": self._tool_write_file,
                "edit_file": self._tool_edit_file,
                "record_evidence": self._tool_record_evidence,
                "query_evidence": self._tool_query_evidence,
                "convert_units": self._tool_convert_units,
                "run_export_audit": self._tool_run_export_audit,
                "save_incumbent": self._tool_save_incumbent,
                "restore_incumbent": self._tool_restore_incumbent,
                "read_image": self._tool_read_image,
            }
            fn = handlers.get(name)
            if fn is None:
                return f"error: unknown tool {name}"
            return clip(await fn(**args))
        except taskbridge.TaskError as exc:
            return f"error: {exc}"
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            return f"error: {type(exc).__name__}: {exc}"

    def _resolve(self, path: str) -> str:
        return path if path.startswith("/") else posixpath.normpath(posixpath.join(self.session.workdir, path))

    def _note_written_path(self, path: str, *, from_bash: bool = False) -> None:
        self._write_seq += 1
        norm = self._resolve(path)
        self.touched_deliverables[norm] = self._write_seq
        if from_bash:
            self.bash_detected_paths.add(norm)
        else:
            self.bash_detected_paths.discard(norm)

    async def _tool_bash(self, command: str, timeout_sec: int = 120) -> str:
        cap = int(self.session.limits.get("exec_timeout_sec") or 600)
        left = self.session.time_left()
        if left is not None:
            cap = min(cap, max(1, int(left - self.finish_margin)))
        res = await self.session.exec(command, taskbridge.positive_int(timeout_sec, 120, cap))
        out = res.get("output") or ""
        if res.get("exit_code") == 0 and any(verb in command for verb in (">", "write", "save", "zip", "build", "render", ".py")):
            for m in OUTPUT_PATH_RE.finditer(f"{command}\n{out}"):
                candidate = self._resolve(m.group(1))
                low_c = candidate.lower()
                if low_c.startswith(("/tmp/", "/var/tmp/")):
                    continue
                if any(part in low_c for part in ("/input/", "/inputs/", "/reference/", "/data/")):
                    continue
                if candidate not in self.touched_deliverables:
                    self._note_written_path(candidate, from_bash=True)
                else:
                    # Refresh write sequence cleanly while preserving existing origin flag
                    self._note_written_path(candidate, from_bash=(candidate in self.bash_detected_paths))
        note = " (timed out)" if res.get("timed_out") else ""
        return f"exit code {res['exit_code']}{note}\n{out or '(no output)'}"

    async def _tool_read_file(self, path: str) -> str:
        data = await self.session.read_file(self._resolve(path))
        if b"\0" in data[:8192]:
            return f"{path} is binary ({len(data)} bytes); inspect via bash or run_export_audit"
        return data.decode("utf-8", "replace") or "(empty file)"

    async def _tool_write_file(self, path: str, content: str, append: bool = False) -> str:
        resolved = self._resolve(path)
        size = await self.session.write_file(resolved, content, append=bool(append))
        self._note_written_path(resolved)
        return f"{path} now holds {size} bytes"

    async def _tool_edit_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        resolved = self._resolve(path)
        text = (await self.session.read_file(resolved)).decode("utf-8")
        count = text.count(old_text) if old_text else 0
        if count == 0:
            return f"error: old_text was not found in {path}"
        if count > 1 and not replace_all:
            return f"error: old_text occurs {count} times in {path}; supply more context or replace_all=true"
        await self.session.write_file(resolved, text.replace(old_text, new_text, -1 if replace_all else 1))
        self._note_written_path(resolved)
        return f"replaced {count if replace_all else 1} occurrence(s) in {path}"

    async def _tool_record_evidence(self, **kwargs: Any) -> str:
        entry = self.ledger.record(**kwargs)
        conflicts = self.ledger.unresolved_conflicts()
        return json.dumps({"recorded": entry, "unresolved_conflicts": list(conflicts.keys())})

    async def _tool_query_evidence(self, key: str = "") -> str:
        return json.dumps(self.ledger.query(key=key), ensure_ascii=False)

    async def _tool_convert_units(self, value: float, from_unit: str, to_unit: str) -> str:
        cat, norm_val, norm_unit = normalize_unit(value, from_unit)
        converted = convert_unit(value, from_unit, to_unit)
        return json.dumps({
            "input": value,
            "from_unit": from_unit,
            "to_unit": to_unit,
            "result": converted,
            "category": cat,
            "normalized_si": {"value": norm_val, "unit": norm_unit},
        })

    async def _tool_read_image(self, path: str) -> str:
        resolved = self._resolve(path)
        data = await self.session.read_file(resolved)
        if len(data) > 5 * 1024 * 1024:
            return f"error: image {path} is {len(data)} bytes (> 5 MiB limit)"
        lower = resolved.lower()
        mime = "image/jpeg" if lower.endswith((".jpg", ".jpeg")) else "image/png"
        b64 = base64.b64encode(data).decode("ascii")
        self._pending_images.append((path, f"data:{mime};base64,{b64}"))
        return f"queued image {path} ({len(data)} bytes) for visual inspection"

    async def _run_audit_internal(
        self,
        path: str,
        *,
        audit_kind: str = "auto",
        narrative_path: str | None = None,
        narrative_text: str = "",
        required_items: list[str] | None = None,
        required_endpoints: dict[str, list[list[float]]] | None = None,
        source_points: dict[str, list[float]] | None = None,
        forbidden_zones: list[dict[str, Any]] | None = None,
        requested_date: str = "",
        prerequisites: list[dict[str, Any]] | None = None,
        expected_work_package: dict[str, Any] | None = None,
        actual_work_package: dict[str, Any] | None = None,
        require_cached_formulas: bool = False,
    ) -> dict[str, Any]:
        resolved = self._resolve(path)
        try:
            raw = await self.session.read_file(resolved)
        except Exception as exc:
            return {"ok": False, "path": resolved, "error": f"read failed: {exc}", "defects": []}

        narr = narrative_text
        if narrative_path:
            try:
                narr = (await self.session.read_file(self._resolve(narrative_path))).decode("utf-8", "replace")
            except Exception as exc:
                return {"ok": False, "path": resolved, "error": f"narrative read failed: {exc}", "defects": []}

        kind = audit_kind
        lower = resolved.lower()
        if kind == "auto":
            if lower.endswith((".xlsx", ".xlsm")):
                kind = "workbook"
            elif lower.endswith(".zip"):
                kind = "zip"
            elif lower.endswith(".ifc"):
                kind = "ifc"
            elif lower.endswith((".docx", ".md", ".txt")):
                kind = "decision_memo"
            else:
                return {"ok": True, "path": resolved, "note": "no specialized binary verifier for extension", "defects": []}

        if kind == "workbook":
            res = audit_workbook_bytes(
                raw,
                required_items=required_items,
                narrative_text=narr,
                require_cached_formulas=bool(require_cached_formulas),
            )
            res["path"] = resolved
            return res
        if kind == "zip":
            try:
                res = audit_zip_deliverables(
                    raw,
                    required_items=required_items,
                    require_cached_formulas=bool(require_cached_formulas),
                )
            except zipfile.BadZipFile as exc:
                return {"ok": False, "path": resolved, "error": f"invalid zip archive: {exc}", "defects": []}
            defects = res.get("defects") or []
            if audit_kind == "auto" and not res.get("ok") and defects and all(d.get("item") == "missing_xlsx" for d in defects):
                with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                    entries = zf.namelist()
                if entries:
                    return {
                        "ok": True,
                        "path": resolved,
                        "zip_entries": entries,
                        "note": "zip archive opened cleanly (no xlsx present)",
                        "defects": [],
                    }
            res["path"] = resolved
            return res
        if kind == "ifc":
            req_ep = (
                {svc: [tuple(float(c) for c in pt) for pt in pts] for svc, pts in required_endpoints.items()}
                if isinstance(required_endpoints, dict)
                else None
            )
            src_ep = (
                {svc: tuple(float(c) for c in pt) for svc, pt in source_points.items()}
                if isinstance(source_points, dict)
                else None
            )
            ifc_res = audit_ifc4_bytes(
                raw,
                required_endpoints=req_ep,
                source_points=src_ep,
                forbidden_zones=forbidden_zones,
            )
            return {
                "ok": ifc_res.ok,
                "path": resolved,
                "element_count": len(ifc_res.elements),
                "cross_service_clashes": ifc_res.cross_service_clashes,
                "same_service_clashes": ifc_res.same_service_clashes,
                "clearance_violations": ifc_res.clearance_violations,
                "unserved_endpoints": ifc_res.unserved_endpoints,
                "forbidden_zone_violations": ifc_res.forbidden_zone_violations,
                "min_cross_clearance": ifc_res.min_cross_clearance,
                "total_route_length": ifc_res.total_route_length,
                "defects": ifc_res.defects,
            }
        if kind == "decision_memo":
            basename = posixpath.basename(lower)
            has_memo_context = bool(
                requested_date
                or prerequisites is not None
                or expected_work_package is not None
                or "memo" in basename
                or "permission" in basename
            )
            sections: tuple[str, ...] = (
                ("decision", "scope", "prerequisite", "change")
                if (audit_kind == "decision_memo" or has_memo_context)
                else ()
            )
            return {
                "path": resolved,
                **audit_decision_memo(
                    raw,
                    is_docx=lower.endswith(".docx"),
                    requested_date=requested_date,
                    prerequisites=prerequisites,
                    expected_work_package=expected_work_package,
                    actual_work_package=actual_work_package,
                    required_sections=sections,
                ),
            }
        return {"ok": False, "path": resolved, "error": f"unknown audit_kind {kind}", "defects": []}

    async def _tool_run_export_audit(self, path: str, **kwargs: Any) -> str:
        res = await self._run_audit_internal(path, **kwargs)
        resolved = self._resolve(path)
        if res.get("ok"):
            self.verified_paths[resolved] = self.touched_deliverables.get(resolved, self._write_seq)
        return json.dumps(res, ensure_ascii=False)

    async def _snapshot_incumbent(self, path: str, note: str) -> str:
        resolved = self._resolve(path)
        data = await self.session.read_file(resolved)
        prev = self.incumbents.get(resolved)
        delta = len(data) - (len(prev[0]) if prev else 0)
        if self.incumbent_total_bytes + delta > MAX_INCUMBENT_BYTES:
            return f"skipped incumbent snapshot for {path}: 20 MiB memory limit reached"
        self.incumbents[resolved] = (data, note)
        self.incumbent_total_bytes += delta
        return f"saved incumbent snapshot for {path} ({len(data)} bytes)"

    async def _tool_save_incumbent(self, path: str, note: str = "") -> str:
        return await self._snapshot_incumbent(path, note)

    async def _tool_restore_incumbent(self, path: str) -> str:
        resolved = self._resolve(path)
        entry = self.incumbents.get(resolved)
        if entry is None:
            return f"error: no incumbent snapshot saved for {path}"
        data, note = entry
        size = await self.session.write_file(resolved, data)
        self._note_written_path(resolved)
        return f"restored {path} ({size} bytes) from incumbent ({note or 'verified'})"

    def unread(self) -> list[dict[str, Any]]:
        last = max((n for n, m in enumerate(self.messages) if m["role"] == "assistant"), default=0)
        return [m for m in self.messages[last:] if m["role"] == "tool"]

    async def compact(self, force: bool = False) -> bool:
        unread_ids = {id(m) for m in self.unread()}
        results = [(n, m) for n, m in enumerate(self.messages) if m["role"] == "tool"]
        cuts, size = [], max(self.estimate_prompt_tokens(), self.prompt_tokens if force else 0)
        for n, m in (results if force else results[:-KEEP_RECENT_RESULTS]):
            if force and self.context_window and size <= self.compact_at():
                break
            content = m.get("content")
            if isinstance(content, str) and len(content) > 600 and id(m) not in unread_ids:
                size -= (len(content) - 400) // 4
                m["content"] = content[:400] + "\n[... trimmed during compaction ...]"
                cuts.append(n)
        if cuts:
            await self.session.log(f"context: trimmed {len(cuts)} older tool outputs")
            self.prompt_tokens = self.estimate_prompt_tokens()
            if not force or not self.context_window or self.prompt_tokens <= self.compact_at():
                return True
        cut = len(self.messages) - 6
        while cut > 3 and self.messages[cut]["role"] == "tool":
            cut -= 1
        if cut < 10 or self.usage_tracker.budget_exhausted():
            return bool(cuts)
        try:
            notes = await self.stream(
                self.messages[:cut] + [{"role": "user", "content": SUMMARY_PROMPT}],
                "none",
                self.finish_margin,
            )
        except (openai.APIError, StreamCut, asyncio.TimeoutError):
            return bool(cuts)
        await self.record(notes)
        text = visible(notes.text)
        if notes.finish is None or not text:
            return bool(cuts)
        self.messages = (
            self.messages[:2]
            + [{"role": "user", "content": "Compacted engineering state:\n" + text}]
            + self.messages[cut:]
        )
        self.prompt_tokens = self.estimate_prompt_tokens()
        return True

    def cut_unread(self) -> bool:
        unread = [
            m for m in self.unread()
            if isinstance(m.get("content"), str) and len(m["content"]) > 2 * UNREAD_KEEP
        ]
        if not unread:
            return False
        share = None
        if self.context_window:
            rest = self.estimate_prompt_tokens() - sum(len(m["content"]) for m in unread) // 4
            share = (self.compact_at() - rest) * 4 // len(unread)
        for m in unread:
            size = len(m["content"])
            keep = size // 2 if share is None or share >= size - 100 else share
            m["content"] = clip(m["content"], max(UNREAD_KEEP, keep))
        self.prompt_tokens = self.estimate_prompt_tokens()
        return True
