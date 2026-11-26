"""Live async WebSocket end-to-end tests of serve.py protocol, SSE streaming, retries, 400 remedy, budget cap, and cancellation."""

from __future__ import annotations

import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import types
from typing import Any
import urllib.error
import urllib.request

from websockets.asyncio.client import connect as ws_connect

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_tcp(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"serve.py did not listen on {port} within {timeout}s")


def _sse_chunk(delta: dict[str, Any], finish_reason: str | None = None, usage: dict | None = None) -> bytes:
    payload: dict[str, Any] = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "google-cloud/gemini-4-neon",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


class _MockOpenAIServer:
    def __init__(self, handler_fn: Any) -> None:
        self.requests: list[dict[str, Any]] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                req_idx = len(parent.requests)
                parent.requests.append(body)
                handler_fn(self, req_idx, body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> _MockOpenAIServer:
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    @staticmethod
    def send_json(handler: BaseHTTPRequestHandler, status: int, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    @staticmethod
    def send_sse(handler: BaseHTTPRequestHandler, frames: list[bytes], delay_sec: float = 0.0) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for frame in frames:
            if delay_sec:
                time.sleep(delay_sec)
            handler.wfile.write(frame)
            handler.wfile.flush()
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()


async def _run_eval_session(
    ws_url: str,
    instruction: str,
    *,
    mcp_servers: list[dict[str, Any]] | None = None,
    cancel_after_sec: float | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    fs: dict[str, bytes] = {"/workspace/input.txt": b"13.8 kV test input\n"}
    usages: list[dict] = []
    execs: list[str] = []
    writes: list[str] = []
    https: list[str] = []

    async with ws_connect(ws_url, max_size=16 << 20) as ws:
        await ws.send(json.dumps({
            "type": "task",
            "instruction": instruction,
            "workdir": "/workspace",
            "limits": {"agent_timeout_sec": 300, "exec_timeout_sec": 60},
            "mcp_servers": mcp_servers or [],
        }))

        cancel_task: asyncio.Task | None = None
        if cancel_after_sec is not None:
            async def _send_cancel() -> None:
                await asyncio.sleep(cancel_after_sec)
                await ws.send(json.dumps({"type": "cancel"}))

            cancel_task = asyncio.create_task(_send_cancel())

        done_msg: dict[str, Any] | None = None
        try:
            async def _loop() -> dict[str, Any]:
                while True:
                    msg = json.loads(await ws.recv())
                    kind = msg.get("type")
                    rid = msg.get("id")
                    if kind == "done":
                        return msg
                    if kind == "error":
                        return msg
                    if kind == "usage":
                        usages.append(msg)
                        continue
                    if kind == "log":
                        continue
                    if kind == "exec":
                        cmd = msg.get("command", "")
                        execs.append(cmd)
                        await ws.send(json.dumps({
                            "type": "result", "id": rid, "ok": True,
                            "exit_code": 0, "output": "executed:" + cmd, "truncated": False, "timed_out": False,
                        }))
                    elif kind == "read_file":
                        p = msg["path"]
                        if p in fs:
                            raw = fs[p]
                            await ws.send(json.dumps({
                                "type": "result", "id": rid, "ok": True,
                                "content_b64": base64.b64encode(raw).decode(), "size": len(raw), "truncated": False,
                            }))
                        else:
                            await ws.send(json.dumps({"type": "result", "id": rid, "ok": False, "error": f"not found: {p}"}))
                    elif kind == "write_file":
                        p = msg["path"]
                        chunk = base64.b64decode(msg["content_b64"])
                        fs[p] = (fs.get(p, b"") + chunk) if msg.get("append") else chunk
                        writes.append(p)
                        await ws.send(json.dumps({"type": "result", "id": rid, "ok": True, "size": len(fs[p])}))
                    elif kind == "http":
                        url = msg["url"]
                        https.append(url)
                        method = msg.get("method", "POST")
                        headers = msg.get("headers") or {}
                        body = base64.b64decode(msg.get("body_b64") or "")

                        def _do_http() -> dict[str, Any]:
                            req = urllib.request.Request(url, data=body or None, method=method, headers=headers)
                            try:
                                with urllib.request.urlopen(req, timeout=10) as resp:
                                    return {
                                        "status": resp.status,
                                        "headers": dict(resp.headers.items()),
                                        "body": resp.read(),
                                    }
                            except urllib.error.HTTPError as err:
                                return {
                                    "status": err.code,
                                    "headers": dict(err.headers.items()),
                                    "body": err.read(),
                                }

                        r = await asyncio.to_thread(_do_http)
                        await ws.send(json.dumps({
                            "type": "result",
                            "id": rid,
                            "ok": True,
                            "status": r["status"],
                            "headers": r["headers"],
                            "body_b64": base64.b64encode(r["body"]).decode(),
                            "truncated": False,
                        }))

            done_msg = await asyncio.wait_for(_loop(), timeout=timeout_sec)
        finally:
            if cancel_task and not cancel_task.done():
                cancel_task.cancel()

    return {"done": done_msg, "fs": fs, "usages": usages, "execs": execs, "writes": writes, "https": https}


def _launch_serve(port: int, openai_url: str, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "PORT": str(port),
        "MODEL_BASE_URL": openai_url,
        "MODEL_API_KEY": "test-key",
        "MODEL_NAME": "google-cloud/gemini-4-neon",
        "RETRY_BASE_SEC": "0.02",
        "BUDGET_CAP_USD": "0",
        "RESERVE_VERIFY_SEC": "10",
    })
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_e2e_tool_roundtrip_retries_and_400_remedy() -> None:
    """Tests startup, 429/500 retries, 400 max_completion_tokens & reasoning_effort remedy, tool round-trip, and done."""
    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        if idx == 0:
            _MockOpenAIServer.send_json(h, 429, {"error": {"message": "rate limit"}})
            return
        if idx == 1:
            _MockOpenAIServer.send_json(h, 500, {"error": {"message": "transient backend error"}})
            return
        if idx == 2:
            # Reject max_completion_tokens -> harness switches to max_tokens
            _MockOpenAIServer.send_json(h, 400, {"error": {"message": "Unsupported parameter: 'max_completion_tokens'"}})
            return
        if idx == 3:
            # Verify harness switched, now reject reasoning_effort via Ollama-style wording -> harness drops it
            assert "max_tokens" in body and "max_completion_tokens" not in body
            _MockOpenAIServer.send_json(h, 400, {"error": {"message": '"llama3.1:8b" does not support thinking'}})
            return
        if idx == 4:
            # Now ask for tool calls (read_file + bash + write_file)
            assert "reasoning_effort" not in body
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [
                        {"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"/workspace/input.txt"}'}},
                        {"index": 1, "id": "c2", "type": "function", "function": {"name": "bash", "arguments": '{"command":"echo checked"}'}},
                        {"index": 2, "id": "c3", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"/workspace/out.txt","content":"verified=ok\\n"}'}},
                    ],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 200, "completion_tokens": 50}),
            ])
            return
        # idx == 5: Final assistant answer
        _MockOpenAIServer.send_sse(h, [
            _sse_chunk({"role": "assistant", "content": "Completed out.txt cleanly after verification."}),
            _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 350, "completion_tokens": 30}),
        ])

    port = _free_port()
    with _MockOpenAIServer(handle) as oai:
        proc = _launch_serve(port, oai.url)
        try:
            _wait_tcp(port, timeout=15.0)
            res = asyncio.run(_run_eval_session(f"ws://127.0.0.1:{port}", "Inspect input and write /workspace/out.txt"))
            assert len(oai.requests) == 6
            assert res["done"]["type"] == "done"
            assert "Completed out.txt cleanly" in res["done"]["final_message"]
            assert res["fs"][b"/workspace/out.txt".decode()] == b"verified=ok\n"
            assert len(res["execs"]) == 1
            assert len(res["usages"]) == 2
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_e2e_budget_cap_and_cancel_preserve_durable_state() -> None:
    """Tests both hard BUDGET_CAP_USD stop (zero extra API calls) and mid-stream cancel handling."""
    # Subtest A: Budget cap $0.05 stops after call #0 ($0.45 > $0.05) without emitting call #1
    def handle_budget(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        _MockOpenAIServer.send_sse(h, [
            _sse_chunk({
                "role": "assistant",
                "content": "Interim checked step before cap.",
                "tool_calls": [
                    {"index": 0, "id": "cb1", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"/workspace/partial.txt","content":"saved"}'}},
                ],
            }),
            _sse_chunk(
                {},
                finish_reason="tool_calls",
                usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000},
            ),
        ])

    port_b = _free_port()
    with _MockOpenAIServer(handle_budget) as oai_b:
        proc_b = _launch_serve(port_b, oai_b.url, {"BUDGET_CAP_USD": "0.05"})
        try:
            _wait_tcp(port_b, timeout=15.0)
            res_b = asyncio.run(_run_eval_session(f"ws://127.0.0.1:{port_b}", "Run within budget cap"))
            assert len(oai_b.requests) == 1
            assert res_b["done"]["type"] == "done"
            assert "Budget cap $0.05 reached" in res_b["done"]["final_message"]
            assert res_b["fs"]["/workspace/partial.txt"] == b"saved"
        finally:
            proc_b.terminate()
            proc_b.wait(timeout=5)

    # Subtest B: Live cancel frame during slow second stream returns done with durable step-1 summary
    def handle_cancel(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        if idx == 0:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "content": "Durable step-1 summary preserved across cancel.",
                    "tool_calls": [
                        {"index": 0, "id": "cc1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"echo step1"}'}},
                    ],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 100, "completion_tokens": 20}),
            ])
            return
        # Second call stalls slowly so evaluator cancel frame hits mid-stream
        _MockOpenAIServer.send_sse(
            h,
            [_sse_chunk({"role": "assistant", "content": "slow..."})],
            delay_sec=4.0,
        )

    port_c = _free_port()
    with _MockOpenAIServer(handle_cancel) as oai_c:
        proc_c = _launch_serve(port_c, oai_c.url)
        try:
            _wait_tcp(port_c, timeout=15.0)
            t0 = time.monotonic()
            res_c = asyncio.run(
                _run_eval_session(
                    f"ws://127.0.0.1:{port_c}",
                    "Do step 1 then long work",
                    cancel_after_sec=0.6,
                    timeout_sec=10.0,
                )
            )
            elapsed = time.monotonic() - t0
            assert res_c["done"]["type"] == "done"
            assert "Durable step-1 summary preserved" in res_c["done"]["final_message"]
            assert elapsed < 9.0
        finally:
            proc_c.terminate()
            proc_c.wait(timeout=5)


def test_e2e_max_model_calls_wrapup() -> None:
    """Tests that MAX_MODEL_CALLS caps tool-loop iterations and triggers a clean finish(tool_choice='none') wrap-up."""
    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        if idx == 0:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [
                        {"index": 0, "id": "m1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"echo step"}'}},
                    ],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 120, "completion_tokens": 25}),
            ])
            return
        # idx == 1: finish() wrap-up must pass tool_choice='none'
        assert body.get("tool_choice") == "none"
        _MockOpenAIServer.send_sse(h, [
            _sse_chunk({"role": "assistant", "content": "Wrapped up cleanly at MAX_MODEL_CALLS=1."}),
            _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 220, "completion_tokens": 20}),
        ])

    port = _free_port()
    with _MockOpenAIServer(handle) as oai:
        proc = _launch_serve(port, oai.url, {"MAX_MODEL_CALLS": "1"})
        try:
            _wait_tcp(port, timeout=15.0)
            res = asyncio.run(_run_eval_session(f"ws://127.0.0.1:{port}", "Run bounded task"))
            assert len(oai.requests) == 2
            assert res["done"]["type"] == "done"
            assert "Wrapped up cleanly at MAX_MODEL_CALLS=1" in res["done"]["final_message"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_e2e_stream_options_400_fallback() -> None:
    """Tests that a 400 rejecting stream_options drops stream_options and completes with reconstructed usage."""
    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        if idx == 0:
            assert "stream_options" in body
            _MockOpenAIServer.send_json(h, 400, {"error": {"message": "Unsupported parameter: 'stream_options'"}})
            return
        assert "stream_options" not in body
        _MockOpenAIServer.send_sse(h, [
            _sse_chunk({"role": "assistant", "content": "Recovered after dropping stream_options."}),
            _sse_chunk({}, finish_reason="stop"),
        ])

    port = _free_port()
    with _MockOpenAIServer(handle) as oai:
        proc = _launch_serve(port, oai.url)
        try:
            _wait_tcp(port, timeout=15.0)
            res = asyncio.run(_run_eval_session(f"ws://127.0.0.1:{port}", "Check stream_options fallback"))
            assert len(oai.requests) == 2
            assert res["done"]["type"] == "done"
            assert "Recovered after dropping stream_options" in res["done"]["final_message"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_e2e_mcp_direct_fast_path_and_parallel_write_deferral() -> None:
    """Tests small-catalog direct MCP tool exposure and deferral of simultaneous write_file/edit_file."""
    secret_word = "MCP-7E31CAFE"

    class MCPHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(length).decode("utf-8"))
            method = msg.get("method")
            params = msg.get("params") or {}
            msg_id = msg.get("id")
            if msg_id is None:
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "apps", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{
                    "name": "secret_word",
                    "description": "Returns the secret word.",
                    "inputSchema": {"type": "object", "properties": {}},
                }]}
            elif method == "tools/call" and params.get("name") == "secret_word":
                result = {"content": [{"type": "text", "text": secret_word}], "isError": False}
            else:
                result = {}
            payload = (b"event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}).encode("utf-8") + b"\n\n")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "mcp-e2e-1")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    mcp_srv = ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
    threading.Thread(target=mcp_srv.serve_forever, daemon=True).start()
    mcp_url = f"http://127.0.0.1:{mcp_srv.server_address[1]}/mcp/"

    def handle_llm(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        tool_names = [t["function"]["name"] for t in body.get("tools", [])]
        assert "apps__secret_word" in tool_names
        assert "mcp_list_servers" not in tool_names
        if idx == 0:
            # Model tries to write a guessed value AND call apps__secret_word in the same turn
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [
                        {"index": 0, "id": "w_premature", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"/workspace/mcp.txt","content":"secret_word"}'}},
                        {"index": 1, "id": "m_direct", "type": "function", "function": {"name": "apps__secret_word", "arguments": "{}"}},
                    ],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 180, "completion_tokens": 30}),
            ])
            return
        if idx == 1:
            # Inspect returned tool messages: premature write_file must be deferred; MCP call succeeded
            tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
            assert len(tool_msgs) == 2
            assert tool_msgs[0]["tool_call_id"] == "w_premature"
            assert tool_msgs[0]["content"].startswith("deferred: inspect the MCP result")
            assert tool_msgs[1]["tool_call_id"] == "m_direct"
            assert secret_word in tool_msgs[1]["content"]
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [
                        {"index": 0, "id": "w_verified", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/workspace/mcp.txt", "content": secret_word})}},
                    ],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 260, "completion_tokens": 25}),
            ])
            return
        _MockOpenAIServer.send_sse(h, [
            _sse_chunk({"role": "assistant", "content": "done"}),
            _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 310, "completion_tokens": 10}),
        ])

    port = _free_port()
    try:
        with _MockOpenAIServer(handle_llm) as oai:
            proc = _launch_serve(port, oai.url, {"MCP_DIRECT_MAX_TOOLS": "8"})
            try:
                _wait_tcp(port, timeout=15.0)
                res = asyncio.run(
                    _run_eval_session(
                        f"ws://127.0.0.1:{port}",
                        "Call apps secret_word and write /workspace/mcp.txt",
                        mcp_servers=[{"name": "apps", "url": mcp_url, "transport": "streamable-http"}],
                    )
                )
                assert len(oai.requests) == 3
                assert res["done"]["type"] == "done"
                assert res["done"]["final_message"] == "done"
                assert res["fs"]["/workspace/mcp.txt"] == secret_word.encode("utf-8")
                assert res["writes"] == ["/workspace/mcp.txt"]
            finally:
                proc.terminate()
                proc.wait(timeout=5)
    finally:
        mcp_srv.shutdown()
        mcp_srv.server_close()


def test_stage2_claude_marked_gemini_signatures_and_audit_wiring(monkeypatch: Any) -> None:
    """Tests Claude breakpoint count <= 4, Stage-2 model config defaults, thought signatures, and DXF/PDF audit wiring."""
    sys.path.insert(0, str(ROOT))
    import ezdxf
    import pypdf
    import harness

    def count_ephemeral(msgs: list[dict[str, Any]]) -> int:
        count = 0
        for m in msgs:
            if isinstance(m.get("cache_control"), dict) and m["cache_control"].get("type") == "ephemeral":
                count += 1
            content = m.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("cache_control"), dict):
                        if part["cache_control"].get("type") == "ephemeral":
                            count += 1
        return count

    convo = [
        {"role": "system", "content": "sys", "cache_control": {"type": "ephemeral"}},
        {"role": "user", "content": [{"type": "text", "text": "u1", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": "a1", "cache_control": {"type": "ephemeral"}},
        {"role": "tool", "tool_call_id": "t1", "content": "tool out"},
        {"role": "user", "content": [{"type": "text", "text": "crop"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
    ]
    marked_msgs = harness.marked(convo, True)
    assert count_ephemeral(marked_msgs) <= 4
    assert count_ephemeral(marked_msgs) == 2

    # Claude Opus 5.5 config compliance
    monkeypatch.setenv("MODEL_NAME", "google-cloud/claude-opus-5-5")
    monkeypatch.delenv("REASONING_EFFORT", raising=False)
    claude_cfg = harness.RuntimeConfig.from_env()
    assert claude_cfg.cache_mark is True
    assert claude_cfg.reasoning_effort == "max"
    assert claude_cfg.responses_api is False

    # Gemini 4 compliance: high default, never thinkingBudget, thought signature preservation
    for gm in ("google-cloud/gemini-4-neon", "google-cloud/gemini-4-carbon", "google-cloud/gemini-4-hydrogen", "google-cloud/gemini-4-argon"):
        monkeypatch.setenv("MODEL_NAME", gm)
        cfg = harness.RuntimeConfig.from_env()
        assert cfg.cache_mark is False
        assert cfg.reasoning_effort == "high"

    reply = harness.Reply()
    delta = types.SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[
            types.SimpleNamespace(
                index=0,
                id="g1",
                function=types.SimpleNamespace(name="bash", arguments='{"command":"ls"}', model_extra={}),
                model_extra={"extra_content": {"google": {"thought_signature": "sig_xyz"}}},
            )
        ],
        model_extra={"provider_specific_fields": {"thought_signature": "turn_sig"}},
    )
    reply.extra = harness.merged(reply.extra, harness.extra_fields(delta, harness.DELTA_FIELDS))
    for p in delta.tool_calls:
        reply.add_call(p)
    msg = reply.message()
    assert msg["provider_specific_fields"]["thought_signature"] == "turn_sig"
    assert msg["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig_xyz"

    # DXF and PDF wiring in EngineeringCoordinator._run_audit_internal
    import io
    doc = ezdxf.new("R2010")
    doc.modelspace().add_line((0, 0), (10, 5))
    buf_dxf = io.StringIO()
    doc.write(buf_dxf)
    dxf_bytes = buf_dxf.getvalue().encode("utf-8")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf_pdf = io.BytesIO()
    writer.write(buf_pdf)
    blank_pdf_bytes = buf_pdf.getvalue()

    class _DummySession:
        workdir = "/workspace"
        mcp_servers: list[dict] = []
        limits: dict = {}
        final_message = ""
        def time_left(self) -> None:
            return None
        async def log(self, text: str) -> None:
            return None
        async def read_file(self, path: str) -> bytes:
            if path.endswith(".dxf"):
                return dxf_bytes
            return blank_pdf_bytes

    coord = harness.EngineeringCoordinator(_DummySession(), config=claude_cfg)
    dxf_res = asyncio.run(coord._run_audit_internal("/workspace/site.dxf"))
    assert dxf_res["ok"] is True
    assert dxf_res["entity_count"] == 1
    pdf_res = asyncio.run(coord._run_audit_internal("/workspace/report.pdf"))
    assert pdf_res["ok"] is False
    assert any(d["item"] == "pdf_empty_text" for d in pdf_res["defects"])
    assert harness._as_bool("false") is False
    assert harness._as_bool("true") is True
