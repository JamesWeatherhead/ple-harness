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
from typing import Any

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
    cancel_after_sec: float | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    fs: dict[str, bytes] = {"/workspace/input.txt": b"13.8 kV test input\n"}
    usages: list[dict] = []
    execs: list[str] = []
    writes: list[str] = []

    async with ws_connect(ws_url, max_size=16 << 20) as ws:
        await ws.send(json.dumps({
            "type": "task",
            "instruction": instruction,
            "workdir": "/workspace",
            "limits": {"agent_timeout_sec": 300, "exec_timeout_sec": 60},
            "mcp_servers": [],
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

            done_msg = await asyncio.wait_for(_loop(), timeout=timeout_sec)
        finally:
            if cancel_task and not cancel_task.done():
                cancel_task.cancel()

    return {"done": done_msg, "fs": fs, "usages": usages, "execs": execs, "writes": writes}


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
            # Verify harness switched, now reject reasoning_effort -> harness drops it
            assert "max_tokens" in body and "max_completion_tokens" not in body
            _MockOpenAIServer.send_json(h, 400, {"error": {"message": "Unknown parameter: reasoning_effort"}})
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
