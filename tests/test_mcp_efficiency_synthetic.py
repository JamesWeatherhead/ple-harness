"""Live local Streamable-HTTP 264-tool MCP server benchmark comparing eager vs on-demand schema discovery."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.request

from mcp_catalog import MCPCatalog


def _build_264_tools() -> list[dict]:
    domains = ("electrical", "mechanical", "civil", "structural", "controls", "permit", "bim", "schedule")
    tools: list[dict] = []
    for idx in range(264):
        dom = domains[idx % len(domains)]
        tools.append({
            "name": f"{dom}_operation_{idx:03d}",
            "description": (
                f"Perform domain operation #{idx:03d} on the {dom} engineering service database, validating inputs, "
                "updating metadata revisions, and returning structured audit status records."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Unique identifier of the engineering project record."},
                    "revision": {"type": "string", "description": "Document or design revision code (e.g. Rev C)."},
                    "target_value": {"type": "number", "description": "Numeric design value to check or update."},
                    "unit": {"type": "string", "description": "Physical unit string associated with target_value."},
                    "dry_run": {"type": "boolean", "description": "Whether to simulate validation without committing."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional engineering discipline tags and cross-reference IDs.",
                    },
                },
                "required": ["project_id", "target_value"],
            },
        })
    return tools


class _HTTPMCPBridgeSession:
    """Minimal Session shim forwarding `session.http()` over real TCP to the local MCP server."""

    def __init__(self, server_url: str) -> None:
        self.mcp_servers = [{"name": "eng_app", "url": server_url}]
        self.logs: list[str] = []

    async def log(self, text: str) -> None:
        self.logs.append(text)

    async def http(
        self,
        url: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        timeout_sec: int = 30,
    ) -> dict:
        def _send() -> dict:
            req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return {
                    "status": resp.status,
                    "headers": dict(resp.headers.items()),
                    "body": resp.read(),
                    "truncated": False,
                }

        return await asyncio.to_thread(_send)


def _start_mcp_server(tools: list[dict]) -> tuple[ThreadingHTTPServer, str]:
    page_size = 60

    class Handler(BaseHTTPRequestHandler):
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
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "eng_app", "version": "1.0"}}
            elif method == "tools/list":
                cursor = int(params.get("cursor") or 0)
                chunk = tools[cursor : cursor + page_size]
                result = {"tools": chunk}
                if cursor + page_size < len(tools):
                    result["nextCursor"] = str(cursor + page_size)
            elif method == "tools/call":
                tname = params.get("name")
                targs = params.get("arguments") or {}
                result = {
                    "content": [{"type": "text", "text": json.dumps({"executed": tname, "args": targs, "status": "ok"})}],
                    "isError": False,
                }
            else:
                result = {}

            payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "sess-264-test")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/mcp"


def test_264_tool_mcp_ondemand_vs_eager_reachability_and_token_reduction() -> None:
    tools = _build_264_tools()
    srv, url = _start_mcp_server(tools)
    try:
        async def run_check() -> None:
            session = _HTTPMCPBridgeSession(url)

            eager = MCPCatalog(mode="eager")
            await eager.connect(session)
            eager_stats = eager.stats()

            ondemand = MCPCatalog(mode="ondemand")
            prompt_note = await ondemand.connect(session)
            od_stats = ondemand.stats()

            assert eager_stats["total_catalog_tools"] == 264
            assert od_stats["total_catalog_tools"] == 264
            assert eager_stats["exposed_tools_count"] == 264
            assert od_stats["exposed_tools_count"] == 4
            assert "eng_app: 264 tools" in prompt_note

            # Expect massive schema overhead reduction (eager ~68k-78k tokens vs ondemand ~400 tokens)
            assert eager_stats["schema_tokens_per_request"] > 50_000
            assert od_stats["schema_tokens_per_request"] < 1_000
            reduction_pct = 100.0 * (1.0 - od_stats["schema_tokens_per_request"] / eager_stats["schema_tokens_per_request"])
            assert reduction_pct > 98.0

            # Verify 264/264 paginated reachability via mcp_list_servers
            discovered: list[str] = []
            offset = 0
            while True:
                page = json.loads(await ondemand.execute_tool("mcp_list_servers", {"offset": offset, "limit": 80}))
                discovered.extend(page["tools"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            assert len(discovered) == 264
            assert len(set(discovered)) == 264

            # Verify search + lazy schema lookup + remote tool execution on the 263rd tool
            search_res = json.loads(await ondemand.execute_tool("mcp_search_tools", {"query": "operation_263"}))
            assert search_res["total_matches"] == 1
            target_tool = search_res["matches"][0]["tool"]

            schema_res = json.loads(await ondemand.execute_tool("mcp_get_tool_schema", {"tools": [target_tool]}))
            assert schema_res["missing"] == []
            assert "target_value" in schema_res["schemas"][0]["parameters"]["properties"]

            call_res = json.loads(
                await ondemand.execute_tool(
                    "mcp_call_tool",
                    {"tool": target_tool, "arguments": {"project_id": "PRJ-01", "target_value": 13.8}},
                )
            )
            assert call_res["status"] == "ok"
            assert call_res["args"]["project_id"] == "PRJ-01"

            final_stats = ondemand.stats()
            assert final_stats["lookup_failures"] == 0
            assert final_stats["successful_calls"] == 1
            assert final_stats["failed_calls"] == 0

        asyncio.run(run_check())
    finally:
        srv.shutdown()
        srv.server_close()


def test_small_catalog_direct_fast_path_threshold() -> None:
    """Tests direct fast-path when <= direct_threshold (default 8) vs 4-tool ondemand when > threshold."""
    small_1 = [{
        "name": "secret_word",
        "description": "Returns the secret word.",
        "inputSchema": {
            "type": "object",
            "properties": {"prefix": {"type": "string", "description": "Optional prefix"}},
            "required": [],
            "additionalProperties": False,
        },
    }]
    srv1, url1 = _start_mcp_server(small_1)
    tools_8 = _build_264_tools()[:8]
    srv8, url8 = _start_mcp_server(tools_8)
    tools_9 = _build_264_tools()[:9]
    srv9, url9 = _start_mcp_server(tools_9)
    try:
        async def run_check() -> None:
            # 1 tool (<= 8): direct fast-path exposes apps__secret_word with full schema properties
            sess1 = _HTTPMCPBridgeSession(url1)
            sess1.mcp_servers = [{"name": "apps", "url": url1}]
            cat1 = MCPCatalog(mode="ondemand", direct_threshold=8)
            note1 = await cat1.connect(sess1)
            exposed1 = cat1.exposed_tools()
            assert len(exposed1) == 1
            assert exposed1[0]["function"]["name"] == "apps__secret_word"
            assert "prefix" in exposed1[0]["function"]["parameters"]["properties"]
            assert exposed1[0]["function"]["parameters"].get("additionalProperties") is False
            assert cat1.is_mcp_tool("apps__secret_word")
            assert "await" in note1.lower() or "wait" in note1.lower()
            direct_res = json.loads(await cat1.execute_tool("apps__secret_word", {"prefix": "X"}))
            assert direct_res["executed"] == "secret_word"
            assert direct_res["args"] == {"prefix": "X"}
            assert cat1.stats()["effective_mode"] == "direct"

            # 8 tools (<= 8): still uses direct fast-path
            cat8 = MCPCatalog(mode="ondemand", direct_threshold=8)
            await cat8.connect(_HTTPMCPBridgeSession(url8))
            assert cat8.stats()["exposed_tools_count"] == 8
            assert cat8.stats()["effective_mode"] == "direct"

            # 9 tools (> 8): switches automatically to 4-tool paginated on-demand mode
            cat9 = MCPCatalog(mode="ondemand", direct_threshold=8)
            await cat9.connect(_HTTPMCPBridgeSession(url9))
            assert cat9.stats()["exposed_tools_count"] == 4
            assert cat9.stats()["effective_mode"] == "ondemand"
            assert [t["function"]["name"] for t in cat9.exposed_tools()] == [
                "mcp_list_servers",
                "mcp_search_tools",
                "mcp_get_tool_schema",
                "mcp_call_tool",
            ]

            # direct_threshold=0 forces 4-tool ondemand even for 1 tool
            cat0 = MCPCatalog(mode="ondemand", direct_threshold=0)
            await cat0.connect(_HTTPMCPBridgeSession(url1))
            assert cat0.stats()["exposed_tools_count"] == 4
            assert cat0.stats()["effective_mode"] == "ondemand"

        asyncio.run(run_check())
    finally:
        for s in (srv1, srv8, srv9):
            s.shutdown()
            s.server_close()
