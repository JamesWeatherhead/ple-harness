# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Streamable-HTTP MCP catalog supporting eager vs. on-demand paginated discovery."""

from __future__ import annotations

import json
import re
from typing import Any

import taskbridge


def unique_name(name: str, taken: dict[str, Any]) -> str:
    """Truncates `name` to 64 chars and appends `_2`, `_3`, ... on collision."""
    candidate, n = name[:64], 1
    while candidate in taken:
        n += 1
        candidate = f"{name[:63 - len(str(n))]}_{n}"
    return candidate


def tool_schema(
    name: str, description: str, required: list[str], **properties: Any
) -> dict[str, Any]:
    """Builds an OpenAI function-tool schema dict."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def estimate_schema_tokens(tools: list[dict[str, Any]]) -> int:
    """Estimates input tokens consumed by `tools` schema JSON (~4 chars/token)."""
    if not tools:
        return 0
    return len(json.dumps(tools, separators=(",", ":"))) // 4


class MCPServerWrapper(taskbridge.MCPClient):
    """Client wrapper for one task-local Streamable-HTTP MCP server."""

    def __init__(self, session: taskbridge.Session, spec: dict[str, Any]):
        super().__init__(session, spec["url"], client="ple-eng-harness")
        self.name = str(spec["name"])
        self.tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        await self.initialize()
        self.tools = await self.list_tools()

    async def invoke(
        self, raw_tool_name: str, arguments: dict[str, Any], max_chars: int = 30_000
    ) -> tuple[str, bool]:
        """Calls `raw_tool_name` on the MCP server and returns `(text, is_ok)`."""
        result = await self.call_tool(raw_tool_name, arguments)
        parts: list[str] = []
        content = result.get("content") if isinstance(result.get("content"), list) else []
        for item in content:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "text":
                parts.append(str(item.get("text", "")))
            elif kind == "resource":
                parts.append(json.dumps(item.get("resource"))[:max_chars])
            elif kind == "resource_link":
                parts.append(json.dumps(item)[:max_chars])
            else:
                parts.append(f"[{kind or 'unknown'} content not shown]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"]))
        text = "\n".join(parts) or "(no content)"
        is_err = bool(result.get("isError"))
        return (f"error: {text}" if is_err else text), (not is_err)


ONDEMAND_CORE_TOOLS = [
    tool_schema(
        "mcp_list_servers",
        "List available task-local MCP application servers and paginated tool names.",
        [],
        server={"type": "string", "description": "Optional server name filter."},
        offset={"type": "integer", "description": "Zero-based pagination offset (default 0)."},
        limit={"type": "integer", "description": "Max tools per page (default 40, max 100)."},
    ),
    tool_schema(
        "mcp_search_tools",
        "Search task-local MCP tools by keyword and paginate matching entries with short descriptions.",
        ["query"],
        query={"type": "string", "description": "Keywords or tool name pattern (empty string lists all)."},
        server={"type": "string", "description": "Optional server filter."},
        offset={"type": "integer", "description": "Pagination offset (default 0)."},
        limit={"type": "integer", "description": "Results per page (default 15, max 40)."},
    ),
    tool_schema(
        "mcp_get_tool_schema",
        "Fetch the full JSON parameter schema for one or more MCP tool names before calling them.",
        ["tools"],
        tools={
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact `<server>__<tool>` names returned by mcp_list_servers or mcp_search_tools.",
        },
    ),
    tool_schema(
        "mcp_call_tool",
        "Execute a task-local MCP tool using its exact `<server>__<tool>` name and JSON arguments.",
        ["tool", "arguments"],
        tool={"type": "string", "description": "Exact `<server>__<tool>` name."},
        arguments={"type": "object", "description": "JSON arguments matching the tool inputSchema."},
    ),
]


class MCPCatalog:
    """Manages task-local MCP servers in `ondemand` (with small-catalog direct fast-path) or `eager` mode."""

    def __init__(
        self,
        mode: str = "ondemand",
        max_direct_tools: int = 128,
        max_direct_chars: int = 40_000,
        direct_threshold: int = 8,
    ):
        self.mode = mode if mode in ("ondemand", "eager") else "ondemand"
        self.max_direct_tools = max_direct_tools
        self.max_direct_chars = max_direct_chars
        self.direct_threshold = max(0, int(direct_threshold))
        self.servers: dict[str, MCPServerWrapper] = {}
        self.registry: dict[str, tuple[MCPServerWrapper, dict[str, Any]]] = {}
        self.direct_functions: list[dict[str, Any]] = []
        self.lookup_failures: int = 0
        self.successful_calls: int = 0
        self.failed_calls: int = 0

    def _build_direct_functions(self) -> list[dict[str, Any]]:
        """Builds OpenAI function schemas for all registered MCP tools, preserving schema properties."""
        funcs: list[dict[str, Any]] = []
        for name, (_, t) in self.registry.items():
            raw_schema = t.get("inputSchema") or {}
            params = {k: v for k, v in raw_schema.items() if k != "$schema"}
            if not params:
                params = {"type": "object", "properties": {}}
            elif params.get("type") == "object" and "properties" not in params:
                params = {**params, "properties": {}}
            funcs.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": (t.get("description") or t["name"])[:1024],
                        "parameters": params,
                    },
                }
            )
        return funcs

    async def connect(self, session: taskbridge.Session) -> str:
        """Initializes all session MCP servers and builds system prompt guidance."""
        self.direct_functions = []
        for spec in session.mcp_servers:
            srv = MCPServerWrapper(session, spec)
            try:
                await srv.start()
            except (taskbridge.TaskError, RuntimeError, ValueError) as exc:
                await session.log(f"MCP server {srv.name} unavailable: {exc}")
                continue
            self.servers[srv.name] = srv
            for t in srv.tools:
                clean = re.sub(r"[^A-Za-z0-9_-]", "_", f"{srv.name}__{t['name']}")
                key = unique_name(clean, self.registry)
                self.registry[key] = (srv, t)
            await session.log(f"MCP server {srv.name}: {len(srv.tools)} tools")

        if not self.registry:
            return ""

        if self.mode == "eager":
            funcs = self._build_direct_functions()
            # Respect OpenAI's hard 128-function ceiling only if provider rejects more,
            # while allowing callers to inspect exact schema size in eager experiments.
            if len(funcs) <= self.max_direct_tools and len(json.dumps(funcs)) <= self.max_direct_chars:
                self.direct_functions = funcs
                return (
                    "\n\nThe task provides MCP application tools directly as `<server>__<tool>` functions. "
                    "Call the needed tool directly and await its returned result before writing files."
                )
            # Even in eager mode if > max_direct_tools we expose all up to max_direct_tools or fallback
            self.direct_functions = funcs
            return (
                f"\n\nThe task provides {len(funcs)} MCP application tools loaded eagerly."
            )

        summary_lines = [
            f"- {sname}: {len(srv.tools)} tools"
            for sname, srv in self.servers.items()
        ]
        if self.direct_threshold > 0 and len(self.registry) <= self.direct_threshold:
            funcs = self._build_direct_functions()
            if len(funcs) <= self.max_direct_tools and len(json.dumps(funcs)) <= self.max_direct_chars:
                self.direct_functions = funcs
                direct_list = ", ".join(f"`{name}`" for name in self.registry)
                return (
                    "\n\nConnected MCP tools exposed directly: "
                    f"{direct_list} ("
                    + ", ".join(f"{sname}: {len(srv.tools)}" for sname, srv in self.servers.items())
                    + "). Invoke the needed MCP tool call directly first, await its returned result, "
                    "and only then call `write_file` or `edit_file` with that exact returned value."
                )

        # On-demand mode: concise server summary only, full schemas fetched lazily
        return (
            "\n\nThe task provides local MCP application services:\n"
            + "\n".join(summary_lines)
            + "\nUse `mcp_list_servers`, `mcp_search_tools`, `mcp_get_tool_schema`, and `mcp_call_tool` "
            "to discover schemas on demand, and await the returned MCP observation before writing files."
        )

    def exposed_tools(self) -> list[dict[str, Any]]:
        """Returns the MCP tool definitions to inject into the LLM `tools` array."""
        if not self.registry:
            return []
        if self.mode == "eager" or self.direct_functions:
            return list(self.direct_functions)
        return list(ONDEMAND_CORE_TOOLS)

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name in {
            "mcp_list_servers",
            "mcp_search_tools",
            "mcp_get_tool_schema",
            "mcp_call_tool",
        } or tool_name in self.registry

    def list_servers(self, server: str = "", offset: int = 0, limit: int = 40) -> str:
        limit = max(1, min(100, int(limit or 40)))
        offset = max(0, int(offset or 0))
        names = [
            name
            for name, (srv, _) in self.registry.items()
            if not server or srv.name == server
        ]
        if server and server not in self.servers:
            self.lookup_failures += 1
            return f"error: unknown server {server!r}; available: {sorted(self.servers)}"
        page = names[offset : offset + limit]
        return json.dumps(
            {
                "servers": {s: len(srv.tools) for s, srv in self.servers.items()},
                "filter_server": server or None,
                "total_tools": len(names),
                "offset": offset,
                "limit": limit,
                "returned": len(page),
                "next_offset": offset + len(page) if offset + len(page) < len(names) else None,
                "tools": page,
            },
            indent=2,
        )

    def search_tools(
        self, query: str, server: str = "", offset: int = 0, limit: int = 15
    ) -> str:
        limit = max(1, min(40, int(limit or 15)))
        offset = max(0, int(offset or 0))
        words = [w for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if w]
        candidates = [
            (name, srv, t)
            for name, (srv, t) in self.registry.items()
            if not server or srv.name == server
        ]
        if words:
            all_matched = []
            any_matched = []
            q_low = (query or "").lower()
            for name, srv, t in candidates:
                desc = (t.get("description") or "").lower()
                nl = name.lower()
                hay = f"{nl} {desc}"
                hits = sum(w in hay for w in words)
                if hits == 0:
                    continue
                score = (
                    (q_low == nl) * 100
                    + (q_low in nl) * 30
                    + sum(w in nl for w in words) * 3
                    + sum(w in desc for w in words)
                )
                entry = (score, name, srv, t)
                if hits == len(words):
                    all_matched.append(entry)
                else:
                    any_matched.append(entry)
            scored = all_matched if all_matched else any_matched
            scored.sort(key=lambda x: (-x[0], x[1]))
            matched = [(n, s, t) for _, n, s, t in scored]
        else:
            matched = candidates

        if not matched:
            self.lookup_failures += 1
            return json.dumps({"total_matches": 0, "matches": [], "hint": "Try mcp_list_servers for exact names."})

        page = matched[offset : offset + limit]
        return json.dumps(
            {
                "total_matches": len(matched),
                "offset": offset,
                "limit": limit,
                "next_offset": offset + len(page) if offset + len(page) < len(matched) else None,
                "matches": [
                    {
                        "tool": name,
                        "server": srv.name,
                        "description": (t.get("description") or t["name"])[:220],
                    }
                    for name, srv, t in page
                ],
            },
            indent=2,
        )

    def get_tool_schema(self, tools: list[str], max_chars: int = 24_000) -> str:
        out: list[dict[str, Any]] = []
        missing: list[str] = []
        for tname in tools:
            if tname not in self.registry:
                missing.append(tname)
                continue
            srv, t = self.registry[tname]
            out.append(
                {
                    "tool": tname,
                    "server": srv.name,
                    "raw_name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": {
                        k: v for k, v in (t.get("inputSchema") or {}).items() if k != "$schema"
                    }
                    or {"type": "object", "properties": {}},
                }
            )
        if missing:
            self.lookup_failures += 1
        payload = json.dumps({"schemas": out, "missing": missing}, indent=2)
        if len(payload) > max_chars:
            payload = payload[:max_chars] + "\n... [truncated; request fewer tools per call]"
        return payload

    async def execute_tool(self, tool_name: str, args: dict[str, Any], max_chars: int = 30_000) -> str:
        if tool_name == "mcp_list_servers":
            return self.list_servers(
                server=str(args.get("server") or ""),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 40),
            )
        if tool_name == "mcp_search_tools":
            return self.search_tools(
                query=str(args.get("query") or ""),
                server=str(args.get("server") or ""),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 15),
            )
        if tool_name == "mcp_get_tool_schema":
            req_tools = args.get("tools") or []
            if isinstance(req_tools, str):
                req_tools = [req_tools]
            return self.get_tool_schema([str(x) for x in req_tools])
        if tool_name == "mcp_call_tool":
            target = str(args.get("tool") or "")
            raw_args = args.get("arguments") or {}
            if isinstance(raw_args, str):
                raw_args = json.loads(raw_args)
            if target not in self.registry:
                self.lookup_failures += 1
                return f"error: unknown tool {target!r}; use mcp_search_tools or mcp_list_servers"
            srv, t = self.registry[target]
            text, ok = await srv.invoke(t["name"], raw_args, max_chars=max_chars)
            if ok:
                self.successful_calls += 1
            else:
                self.failed_calls += 1
            return text
        if tool_name in self.registry:
            srv, t = self.registry[tool_name]
            text, ok = await srv.invoke(t["name"], args, max_chars=max_chars)
            if ok:
                self.successful_calls += 1
            else:
                self.failed_calls += 1
            return text
        self.lookup_failures += 1
        return f"error: unknown MCP tool {tool_name!r}"

    def stats(self) -> dict[str, Any]:
        effective_mode = "direct" if (self.mode != "eager" and self.direct_functions) else self.mode
        return {
            "mode": self.mode,
            "effective_mode": effective_mode,
            "direct_threshold": self.direct_threshold,
            "servers": len(self.servers),
            "total_catalog_tools": len(self.registry),
            "exposed_tools_count": len(self.exposed_tools()),
            "schema_tokens_per_request": estimate_schema_tokens(self.exposed_tools()),
            "lookup_failures": self.lookup_failures,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
        }
