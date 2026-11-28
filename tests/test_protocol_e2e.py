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
    initial_files: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    fs: dict[str, bytes] = {"/workspace/input.txt": b"13.8 kV test input\n"}
    if initial_files:
        fs.update(initial_files)
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


def test_e2e_effort_backoff_milestone_guard_and_budget_warn() -> None:
    """Tests max->high effort backoff on empty length cutoff, milestone guard at call 10, and call budget warning."""
    seen_efforts: list[str | None] = []
    seen_milestone: list[bool] = []
    seen_budget_warn: list[bool] = []

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        seen_efforts.append(body.get("reasoning_effort"))
        all_txt = "\n".join(str(m.get("content") or "") for m in (body.get("messages") or []))
        seen_milestone.append("MILESTONE" in all_txt)
        seen_budget_warn.append("CALL BUDGET WARNING" in all_txt or "remaining" in all_txt.lower())
        if idx == 0:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "reasoning": "thinking hit output token ceiling..."}),
                _sse_chunk({}, finish_reason="length", usage={"prompt_tokens": 40, "completion_tokens": 50}),
            ])
        elif idx < 10:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": f"call_work_{idx}",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": json.dumps({"path": f"/workspace/work/s{idx}.py", "content": "pass"})},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])
        elif idx == 10:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_deliv",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path":"/workspace/output/report.md","content":"# Report\\nVerified result.\\n"}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])
        else:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Verified output/report.md."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])

    port = _free_port()
    with _MockOpenAIServer(handle) as oai:
        proc = _launch_serve(port, oai.url, {"REASONING_EFFORT": "max", "MAX_MODEL_CALLS": "13"})
        try:
            _wait_tcp(port, timeout=15.0)
            res = asyncio.run(_run_eval_session(f"ws://127.0.0.1:{port}", "Write /workspace/output/report.md"))
            assert res["done"]["type"] == "done"
            assert seen_efforts[0] == "max"
            assert seen_efforts[1] == "high"
            assert any(seen_milestone), "Expected HARNESS MILESTONE injection at call 10 while only work/*.py existed"
            assert any(seen_budget_warn), "Expected CALL BUDGET WARNING near MAX_MODEL_CALLS"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_e2e_low_effort_drops_reasoning_and_rejects_dsml_leak() -> None:
    """Tests that low->None effort backoff occurs on hidden length cutoff and raw DSML XML is nudged back."""
    seen_payloads: list[dict] = []

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        seen_payloads.append(body)
        if idx == 0:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "reasoning": "low effort still truncated..."}),
                _sse_chunk({}, finish_reason="length", usage={"prompt_tokens": 30, "completion_tokens": 50}),
            ])
        elif idx == 1:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": '<｜DSML｜ calls>\n<｜DSML｜ invoke name="bash">'}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 40, "completion_tokens": 20}),
            ])
        elif idx == 2:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_dsml_rec",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path":"/workspace/out.txt","content":"clean"}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])
        else:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Wrote /workspace/out.txt cleanly."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 50, "completion_tokens": 10}),
            ])

    port = _free_port()
    with _MockOpenAIServer(handle) as oai:
        proc = _launch_serve(port, oai.url, {"REASONING_EFFORT": "low"})
        try:
            _wait_tcp(port, timeout=15.0)
            res = asyncio.run(_run_eval_session(f"ws://127.0.0.1:{port}", "Write /workspace/out.txt"))
            assert len(seen_payloads) == 4
            assert seen_payloads[0].get("reasoning_effort") == "low"
            assert "reasoning_effort" not in seen_payloads[1]
            assert res["fs"]["/workspace/out.txt"] == b"clean"
            assert "Wrote /workspace/out.txt cleanly." in res["done"]["final_message"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_e2e_run_export_audit_mismatched_docx_as_workbook_no_crash() -> None:
    """Tests that run_export_audit(audit_kind='workbook') on .docx returns ok=False without crashing."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    docx_bytes = buf.getvalue()

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        if idx == 0:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_docx_as_wb",
                        "type": "function",
                        "function": {
                            "name": "run_export_audit",
                            "arguments": '{"path":"/workspace/study.docx","audit_kind":"workbook"}',
                        },
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])
        elif idx == 1:
            msgs = body.get("messages") or []
            tool_res = [json.loads(m["content"].splitlines()[0]) for m in msgs if m.get("role") == "tool"]
            assert len(tool_res) == 1
            assert tool_res[0]["ok"] is False and "not an Excel workbook" in tool_res[0]["error"]
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_recover_docx",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path":"/workspace/recovered.txt","content":"survived"}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 60, "completion_tokens": 15}),
            ])
        else:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Recovered after docx audit mismatch."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 70, "completion_tokens": 10}),
            ])

    port = _free_port()
    with _MockOpenAIServer(handle) as oai:
        proc = _launch_serve(port, oai.url)
        try:
            _wait_tcp(port, timeout=15.0)
            res = asyncio.run(
                _run_eval_session(
                    f"ws://127.0.0.1:{port}",
                    "Audit docx file",
                    initial_files={"/workspace/study.docx": docx_bytes},
                )
            )
            assert res["done"]["type"] == "done"
            assert res["fs"]["/workspace/recovered.txt"] == b"survived"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_e2e_run_export_audit_corrupt_xlsx_no_crash() -> None:
    """Tests that run_export_audit on a corrupt .xlsx returns ok=False without crashing run_tool."""

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        if idx == 0:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_bad_xlsx",
                        "type": "function",
                        "function": {
                            "name": "run_export_audit",
                            "arguments": '{"path":"/workspace/corrupt.xlsx","audit_kind":"workbook"}',
                        },
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])
        elif idx == 1:
            msgs = body.get("messages") or []
            tool_res = [json.loads(m["content"].splitlines()[0]) for m in msgs if m.get("role") == "tool"]
            assert len(tool_res) == 1
            assert tool_res[0]["ok"] is False
            assert tool_res[0].get("error") or tool_res[0].get("defects")
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_recover_xlsx",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path":"/workspace/fixed.txt","content":"ok"}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 60, "completion_tokens": 15}),
            ])
        else:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Recovered after corrupt xlsx."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 70, "completion_tokens": 10}),
            ])

    port = _free_port()
    with _MockOpenAIServer(handle) as oai:
        proc = _launch_serve(port, oai.url)
        try:
            _wait_tcp(port, timeout=15.0)
            res = asyncio.run(
                _run_eval_session(
                    f"ws://127.0.0.1:{port}",
                    "Audit corrupt xlsx",
                    initial_files={"/workspace/corrupt.xlsx": b"not-a-zip-xlsx"},
                )
            )
            assert res["done"]["type"] == "done"
            assert res["fs"]["/workspace/fixed.txt"] == b"ok"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_template_schema_mismatch_and_docx_only_zip() -> None:
    """Tests template_schemas missing column/sheet detection and require_xlsx=False for DOCX-only ZIPs."""
    import io
    import zipfile
    import openpyxl

    sys.path.insert(0, str(ROOT))
    from verifiers.workbook_audit import audit_workbook_bytes, audit_zip_deliverables

    wb = openpyxl.Workbook()
    ws_items = wb.active
    ws_items.title = "Items"
    ws_items.append(["item_id", "total_project_cost_usd"])
    ws_items.append(["C01", 1000.0])
    ws_seg = wb.create_sheet("Segments")
    ws_seg.append(["seg_id", "length_ft"])
    ws_seg.append(["S1", 100])
    buf_xlsx = io.BytesIO()
    wb.save(buf_xlsx)
    xlsx_bytes = buf_xlsx.getvalue()

    tmpl = {
        "templates/conveyance_study_TEMPLATE.xlsx": {
            "Items": ["item_id", "total_project_cost_usd"],
            "Segments": ["seg_id", "length_ft", "reinforced", "soil_type", "protective_system", "slope_h_per_v"],
            "Alternatives": ["item_id"],
        }
    }
    res_wb = audit_workbook_bytes(xlsx_bytes, template_schemas=tmpl)
    assert res_wb["ok"] is False
    items = {d["item"] for d in res_wb["defects"]}
    assert "template_missing_sheet:Alternatives" in items
    assert "template_missing_columns:Segments" in items

    # DOCX-only ZIP passes cleanly with require_xlsx=False
    buf_docx = io.BytesIO()
    with zipfile.ZipFile(buf_docx, "w") as zf_doc:
        zf_doc.writestr("[Content_Types].xml", "<Types/>")
        zf_doc.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Division 26 Spec</w:t></w:r></w:p></w:body></w:document>")
    buf_zip = io.BytesIO()
    with zipfile.ZipFile(buf_zip, "w") as zf_out:
        zf_out.writestr("specs/26_05_13.docx", buf_docx.getvalue())
    res_zip = audit_zip_deliverables(buf_zip.getvalue(), require_xlsx=False, template_schemas=tmpl)
    assert res_zip["ok"] is True


def test_bash_ignores_readonly_inputs_and_memo_does_not_force_pto_sections(monkeypatch: Any) -> None:
    """Tests that _tool_bash skips read-only input paths and validation_memo.docx skips PTO section checks."""
    import io
    import zipfile

    sys.path.insert(0, str(ROOT))
    import harness

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    cfg = harness.RuntimeConfig.from_env()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>E119 validation test summary.</w:t></w:r></w:p></w:body></w:document>")
    memo_docx = buf.getvalue()

    class _DummySession:
        workdir = "/workspace"
        mcp_servers: list[dict] = []
        limits: dict = {}
        final_message = ""
        def time_left(self) -> None:
            return None
        async def log(self, text: str) -> None:
            return None
        async def exec(self, command: str, timeout_sec: int = 120) -> dict[str, Any]:
            return {"exit_code": 0, "output": "inspected specs/DC-02_Mechanical_Design_Criteria_Memorandum_Rev3.docx", "timed_out": False}
        async def read_file(self, path: str) -> bytes:
            return memo_docx

    coord = harness.EngineeringCoordinator(_DummySession(), config=cfg)
    coord._initial_files.add("/workspace/existing_note.md")
    asyncio.run(coord._tool_bash("python3 read.py > specs/DC-02_Mechanical_Design_Criteria_Memorandum_Rev3.docx"))
    asyncio.run(coord._tool_bash("cat existing_note.md > existing_note.md"))
    assert "/workspace/specs/DC-02_Mechanical_Design_Criteria_Memorandum_Rev3.docx" not in coord.touched_deliverables
    assert "/workspace/existing_note.md" not in coord.touched_deliverables

    res_memo = asyncio.run(coord._run_audit_internal("/workspace/output/E119_validation_memo.docx"))
    assert res_memo["ok"] is True


def test_workspace_recon_injects_brief_and_fires_oneshot_unread_spec_gate(monkeypatch: Any) -> None:
    """Tests ENABLE_WORKSPACE_RECON parses Turn-0 payload, injects brief/schemas, and gates unread specs once."""
    sys.path.insert(0, str(ROOT))
    import harness
    import openai

    recon_payload = {
        "total_files": 4,
        "files": [
            {"path": "templates/conveyance_study_TEMPLATE.xlsx", "size": 4096},
            {"path": "criteria/design_criteria.pdf", "size": 2048},
            {"path": "specs/utility_spec.docx", "size": 1024},
        ],
        "templates": [{
            "path": "templates/conveyance_study_TEMPLATE.xlsx",
            "sheets": {"Segments": {"rows": 5, "headers": ["seg_id", "soil_type"], "col_a_samples": ["S1"]}},
        }],
        "json_schemas": [],
        "spec_files": ["criteria/design_criteria.pdf", "specs/utility_spec.docx"],
        "packages": {"openpyxl": True, "ifcopenshell": False},
    }
    seen_prompts: list[str] = []

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        msgs = body.get("messages") or []
        seen_prompts.append(json.dumps(msgs))
        if idx == 0:
            # First turn: write output without reading any spec files -> should trigger unread-spec gate once
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_w",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path":"/workspace/output/out.md","content":"draft"}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])
        elif idx == 1:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Done without reading specs."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 60, "completion_tokens": 10}),
            ])
        elif idx == 2:
            # After gate asks to read specs, read one via read_file
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_r",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"/workspace/specs/utility_spec.docx"}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 70, "completion_tokens": 10}),
            ])
        else:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Verified after reading specs."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 80, "completion_tokens": 10}),
            ])

    class _ReconSession:
        workdir = "/workspace"
        instruction = "Complete task using templates and specs."
        mcp_servers: list[dict] = []
        limits: dict = {"exec_timeout_sec": 60}
        final_message = ""
        fs: dict[str, bytes] = {"/workspace/specs/utility_spec.docx": b"Voltage limit 13.8kV"}
        def time_left(self) -> float:
            return 1000.0
        async def log(self, text: str) -> None:
            return None
        async def usage(self, model: str, i: int, o: int, c: int) -> None:
            return None
        async def exec(self, command: str, timeout_sec: int = 120) -> dict[str, Any]:
            return {"exit_code": 0, "output": f"===RECON_START===\n{json.dumps(recon_payload)}\n===RECON_END===", "timed_out": False}
        async def read_file(self, path: str) -> bytes:
            return self.fs[path]
        async def write_file(self, path: str, content: str | bytes, append: bool = False) -> int:
            raw = content.encode() if isinstance(content, str) else content
            self.fs[path] = raw
            return len(raw)

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_WORKSPACE_RECON", "true")
    monkeypatch.setenv("MODEL_NAME", "openai/gpt-oss-120b")
    with _MockOpenAIServer(handle) as oai:
        cfg = harness.RuntimeConfig.from_env()
        client = openai.AsyncOpenAI(base_url=oai.url, api_key="test-key")
        coord = harness.EngineeringCoordinator(_ReconSession(), config=cfg, client=client)
        final_out = asyncio.run(coord.run())
        assert "Verified after reading specs." in final_out
        assert "[WORKSPACE BRIEF" in seen_prompts[0]
        assert "soil_type" in seen_prompts[0]
        assert coord._template_schemas["templates/conveyance_study_TEMPLATE.xlsx"]["Segments"] == ["seg_id", "soil_type"]
        assert coord._spec_gate_fired is True
        assert coord._spec_reads_seen is True
        assert "No project spec/criteria documents" in seen_prompts[2]


def _make_multiservice_mcp_session() -> Any:
    """Builds an in-process fake MCP session exposing 12 tools across multiple Cluster-A groups."""
    tools = [
        {
            "name": "openproject_mcp_list_projects",
            "description": "List OpenProject projects.",
            "inputSchema": {"type": "object", "properties": {"offset": {"type": "integer"}}, "required": []},
        },
        {
            "name": "openproject_mcp_create_work_package",
            "description": "Create an OpenProject work package for case tracking.",
            "inputSchema": {
                "type": "object",
                "properties": {"project_id": {"type": "integer"}, "subject": {"type": "string"}},
                "required": ["project_id", "subject"],
            },
        },
        {"name": "mail_list_messages", "description": "List mailbox messages.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
        {"name": "mail_send_message", "description": "Send email message.", "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}},
        {"name": "wiki_js_mcp_search_pages", "description": "Search Wiki.js pages.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
        {"name": "wiki_js_mcp_get_page", "description": "Read a Wiki.js page.", "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}}},
        {"name": "calendar_v2_list_events", "description": "List calendar events.", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "calendar_v2_create_event", "description": "Create calendar event.", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "mysql_execute_sql", "description": "Execute SQL query.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}}},
        {"name": "nextcloud_mcp_list_files", "description": "List Nextcloud files.", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "excel_v2_read", "description": "Read workbook.", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "word_v2_create_document", "description": "Create docx document.", "inputSchema": {"type": "object", "properties": {}}},
    ]

    class _FakeMCPSession:
        workdir = "/filesystem"
        instruction = "Prepare the interconnection response memo and close case tracking."
        mcp_servers = [{"name": "apps", "url": "http://127.0.0.1:8000/mcp/"}]
        limits = {"exec_timeout_sec": 60}
        final_message = ""

        def time_left(self) -> float:
            return 1000.0

        async def log(self, text: str) -> None:
            return None

        async def usage(self, model: str, i: int, o: int, c: int) -> None:
            return None

        async def http(self, url: str, method: str = "POST", headers=None, body: bytes = b"", timeout_sec: int = 30) -> dict:
            msg = json.loads(body.decode("utf-8"))
            method_name = msg.get("method")
            msg_id = msg.get("id")
            if msg_id is None:
                return {"status": 202, "headers": {}, "body": b""}
            if method_name == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
            elif method_name == "tools/list":
                result = {"tools": tools}
            else:
                result = {}
            payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}).encode("utf-8")
            return {"status": 200, "headers": {"content-type": "application/json"}, "body": payload}

    return _FakeMCPSession()


def test_ondemand_catalog_surfaces_service_groups_and_inline_schemas() -> None:
    """Tests that on-demand MCP discovery indexes service groups and returns argument schemas on search."""
    from mcp_catalog import MCPCatalog

    sess = _make_multiservice_mcp_session()
    cat = MCPCatalog(mode="ondemand", direct_threshold=8)
    prompt = asyncio.run(cat.connect(sess))
    assert "Available MCP service groups:" in prompt
    for grp in ("openproject_mcp", "mail", "wiki_js_mcp"):
        assert grp in prompt
    listed = json.loads(cat.list_servers())
    assert listed["service_groups"]["openproject_mcp"] == 2
    assert listed["service_groups"]["mail"] == 2
    assert listed["service_groups"]["wiki_js_mcp"] == 2
    searched = json.loads(cat.search_tools("openproject"))
    assert len(searched["matches"]) == 2
    create_entry = next(m for m in searched["matches"] if m["tool"].endswith("create_work_package"))
    assert create_entry["required_args"] == ["project_id", "subject"]
    assert set(create_entry["arg_names"]) == {"project_id", "subject"}
    assert "parameters" in create_entry
    assert create_entry["parameters"]["required"] == ["project_id", "subject"]


def test_pre_completion_gate_surfaces_both_mcp_groups_and_missing_deliverable(monkeypatch) -> None:
    """Tests that zero-MCP and empty-deliverable checks fire simultaneously with service-group hints."""
    import harness
    import openai

    sess = _make_multiservice_mcp_session()
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "google-cloud/gpt-oss-120b")
    cfg = harness.RuntimeConfig.from_env()
    client = openai.AsyncOpenAI(base_url="http://127.0.0.1:9/v1", api_key="test-key")
    coord = harness.EngineeringCoordinator(sess, config=cfg, client=client)
    asyncio.run(coord.catalog.connect(sess))
    assert len(coord.catalog.registry) == 12
    assert coord.catalog.successful_calls == 0

    gate_msg = asyncio.run(coord._pre_completion_gate("Summary claiming memo written."))
    assert gate_msg is not None
    assert "Connected MCP services (" in gate_msg
    assert "openproject_mcp" in gate_msg
    assert "mail" in gate_msg
    assert "No deliverable file has been written on disk yet" in gate_msg


def test_v3_mcp_nonblocking_write_and_oneshot_nudge(monkeypatch: Any) -> None:
    """Tests that standalone write_file is never blocked when MCP has 0 calls and MCP gate fires only once."""
    sys.path.insert(0, str(ROOT))
    import harness
    import openai

    sess = _make_multiservice_mcp_session()
    sess.fs: dict[str, bytes] = {}

    async def write_file(path: str, data: str | bytes, append: bool = False) -> int:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        sess.fs[path] = raw
        return len(raw)

    async def read_file(path: str) -> bytes:
        return sess.fs[path]

    sess.write_file = write_file
    sess.read_file = read_file

    seen_reqs: list[dict] = []

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        seen_reqs.append(body)
        if idx == 0:
            # Standalone write_file without calling MCP -> must NOT be deferred in v3.0
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_w_direct",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path":"/filesystem/memo.md","content":"Complete technical memo draft."}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}),
            ])
        elif idx == 1:
            # Verify write_file succeeded immediately
            tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
            assert any("now holds" in str(m.get("content")) for m in tool_msgs)
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Memo written on disk."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 60, "completion_tokens": 10}),
            ])
        else:
            # One-shot MCP reminder fired once -> even if model still finishes without MCP, loop accepts instead of stalling
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Final memo confirmed without redundant MCP churn."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 70, "completion_tokens": 10}),
            ])

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "google-cloud/gpt-oss-120b")
    monkeypatch.setenv("ENABLE_INDEPENDENT_REVIEWER", "false")
    with _MockOpenAIServer(handle) as oai:
        cfg = harness.RuntimeConfig.from_env()
        client = openai.AsyncOpenAI(base_url=oai.url, api_key="test-key")
        coord = harness.EngineeringCoordinator(sess, config=cfg, client=client)
        out = asyncio.run(coord.run())
        assert "Final memo confirmed" in out
        assert coord._mcp_nudge_fired is True
        assert sess.fs["/filesystem/memo.md"] == b"Complete technical memo draft."
        assert len(seen_reqs) == 3


def test_v3_independent_pe_reviewer_catches_defect_and_approves(monkeypatch: Any) -> None:
    """Tests fresh-context PE reviewer finding a defect, coordinator fixing it, and reviewer approving PASS."""
    sys.path.insert(0, str(ROOT))
    import harness
    import openai

    class _ReviewSession:
        workdir = "/workspace"
        instruction = "Produce /workspace/design.md with Load Factor >= 1.5."
        mcp_servers: list[dict] = []
        limits = {"exec_timeout_sec": 60}
        final_message = ""
        fs: dict[str, bytes] = {}

        def time_left(self) -> float:
            return 3600.0

        async def log(self, text: str) -> None:
            return None

        async def usage(self, model: str, i: int, o: int, c: int) -> None:
            return None

        async def exec(self, command: str, timeout_sec: int = 120) -> dict[str, Any]:
            return {"exit_code": 0, "output": "ok", "timed_out": False}

        async def read_file(self, path: str) -> bytes:
            return self.fs[path]

        async def write_file(self, path: str, data: str | bytes, append: bool = False) -> int:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            self.fs[path] = raw
            return len(raw)

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        sys_msg = (body.get("messages") or [{}])[0].get("content", "")
        is_reviewer = "Senior Principal Engineer" in sys_msg
        if not is_reviewer:
            if idx == 0:
                _MockOpenAIServer.send_sse(h, [
                    _sse_chunk({
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": "w1",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":"/workspace/design.md","content":"Load Factor: 1.1"}'},
                        }],
                    }),
                    _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 15}),
                ])
            elif idx == 1:
                _MockOpenAIServer.send_sse(h, [
                    _sse_chunk({"role": "assistant", "content": "Initial design written."}),
                    _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 60, "completion_tokens": 10}),
                ])
            elif idx == 4:
                # Fix defect flagged by reviewer
                _MockOpenAIServer.send_sse(h, [
                    _sse_chunk({
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": "w2",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":"/workspace/design.md","content":"Load Factor: 1.65 (PASS)"}'},
                        }],
                    }),
                    _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 90, "completion_tokens": 15}),
                ])
            else:
                _MockOpenAIServer.send_sse(h, [
                    _sse_chunk({"role": "assistant", "content": "Updated Load Factor to 1.65."}),
                    _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 100, "completion_tokens": 10}),
                ])
        else:
            # Reviewer: inspect file once, flag 1.1 -> later approve PASS when updated to 1.65
            tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
            if not tool_msgs:
                _MockOpenAIServer.send_sse(h, [
                    _sse_chunk({
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": f"rev_r_{idx}",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"/workspace/design.md"}'},
                        }],
                    }),
                    _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 40, "completion_tokens": 12}),
                ])
            elif "1.65" in str(tool_msgs[-1].get("content")):
                _MockOpenAIServer.send_sse(h, [
                    _sse_chunk({"role": "assistant", "content": "PASS"}),
                    _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 60, "completion_tokens": 5}),
                ])
            else:
                _MockOpenAIServer.send_sse(h, [
                    _sse_chunk({"role": "assistant", "content": "1. /workspace/design.md Load Factor is 1.1 (< 1.5 required)."}),
                    _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 60, "completion_tokens": 15}),
                ])

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "google-cloud/gemini-4-neon")
    monkeypatch.setenv("ENABLE_INDEPENDENT_REVIEWER", "true")
    monkeypatch.setenv("MAX_REVIEW_ROUNDS", "2")
    with _MockOpenAIServer(handle) as oai:
        cfg = harness.RuntimeConfig.from_env()
        client = openai.AsyncOpenAI(base_url=oai.url, api_key="test-key")
        sess = _ReviewSession()
        coord = harness.EngineeringCoordinator(sess, config=cfg, client=client)
        out = asyncio.run(coord.run())
        assert "1.65" in out
        assert b"1.65" in sess.fs["/workspace/design.md"]


def test_v3_image_input_auto_fallback_on_400(monkeypatch: Any) -> None:
    """Tests drop_image_input strips image_url and removes read_image when endpoint rejects vision."""
    sys.path.insert(0, str(ROOT))
    import harness
    import openai

    tiny_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

    class _ImgSession:
        workdir = "/workspace"
        instruction = "Check drawing.png and finish."
        mcp_servers: list[dict] = []
        limits = {"exec_timeout_sec": 60}
        final_message = ""
        fs: dict[str, bytes] = {"/workspace/drawing.png": tiny_png}

        def time_left(self) -> float:
            return 200.0

        async def log(self, text: str) -> None:
            return None

        async def usage(self, model: str, i: int, o: int, c: int) -> None:
            return None

        async def read_file(self, path: str) -> bytes:
            return self.fs[path]

        async def write_file(self, path: str, data: str | bytes, append: bool = False) -> int:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            self.fs[path] = raw
            return len(raw)

    def handle(h: BaseHTTPRequestHandler, idx: int, body: dict) -> None:
        if idx == 0:
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "img1",
                        "type": "function",
                        "function": {"name": "read_image", "arguments": '{"path":"/workspace/drawing.png"}'},
                    }],
                }),
                _sse_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 40, "completion_tokens": 12}),
            ])
        elif idx == 1:
            _MockOpenAIServer.send_json(h, 400, {"error": {"message": "This model does not support image_url inputs"}})
        else:
            tool_names = [t["function"]["name"] for t in body.get("tools") or []]
            assert "read_image" not in tool_names
            _MockOpenAIServer.send_sse(h, [
                _sse_chunk({"role": "assistant", "content": "Fallback text parsing completed."}),
                _sse_chunk({}, finish_reason="stop", usage={"prompt_tokens": 50, "completion_tokens": 10}),
            ])

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "google-cloud/gpt-oss-120b")
    monkeypatch.setenv("ENABLE_IMAGE_INPUT", "true")
    monkeypatch.setenv("ENABLE_INDEPENDENT_REVIEWER", "false")
    with _MockOpenAIServer(handle) as oai:
        cfg = harness.RuntimeConfig.from_env()
        client = openai.AsyncOpenAI(base_url=oai.url, api_key="test-key")
        coord = harness.EngineeringCoordinator(_ImgSession(), config=cfg, client=client)
        out = asyncio.run(coord.run())
        assert "Fallback text parsing completed." in out
        assert coord._image_input_disabled is True
        assert all(t["function"]["name"] != "read_image" for t in coord.tools)

