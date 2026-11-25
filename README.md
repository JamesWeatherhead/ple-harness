# PLE Engineering Coordinator (`ple-harness`)

Open-source entry for Google's **Physical-Engineering's Last Exam (PLE)** competition, licensed under **Apache-2.0**.

Built on top of the upstream `google-research/ple` `taskbridge.py` WebSocket serve protocol and clean OpenAI-compatible SSE/Responses streaming architecture (derived with attribution from `sample_submissions/custom-openai`).

## Architecture

- [`serve.py`](serve.py) — Thin WebSocket entrypoint via `taskbridge.run(solve)`.
- [`harness.py`](harness.py) — Single-coordinator engineering loop with Gemini thought-signature preservation, Claude signed `thinking_blocks` + ephemeral prompt caching, OpenAI Responses API fallback, context compaction, and `BUDGET_CAP_USD` enforcement.
- [`mcp_catalog.py`](mcp_catalog.py) — Streamable-HTTP MCP catalog supporting `ondemand` paginated discovery (`mcp_list_servers`, `mcp_search_tools`, `mcp_get_tool_schema`, `mcp_call_tool`) vs `eager` mode (eliminating ~75k tokens/turn schema overhead on large 264-tool MCP environments).
- [`evidence.py`](evidence.py) — Keyed `EvidenceLedger` with unit normalization/conversion and active conflict tracking (`supersedes`).
- [`verifiers/`](verifiers/) — Deterministic cold-read auditors for XLSX/ZIP deliverables, 2D viewport calibration & STEP IFC4 3D geometry/clash/clearance/connectivity checks, and decision memos (`DOCX`/`MD`).
- [`pricing.py`](pricing.py) — Stage 2 per-model token pricing and explicit measured-vs-reconstructed cost accounting.

## Ablation / Feature Flags (`submission.yaml`)

All environment overrides use string values:
- `MCP_DISCOVERY_MODE`: `"ondemand"` (default) or `"eager"`
- `ENABLE_EVIDENCE_LEDGER`: `"true"` (default) or `"false"`
- `ENABLE_EXPORT_VERIFIER`: `"true"` (default) or `"false"`
- `ENABLE_INCUMBENT_RETENTION`: `"true"` (default) or `"false"`
- `ENABLE_IMAGE_INPUT`: `"false"` (default) or `"true"`
- `BUDGET_CAP_USD`: `"0"` (unlimited) or e.g. `"2.50"`
- `RESERVE_VERIFY_SEC`: `"150"`
- `MAX_VERIFY_REPAIRS`: `"2"`

## Attribution

Includes `taskbridge.py` and streaming/compaction design adapted from `google-research/ple` (`Apache-2.0`, upstream commit `5cf736f8b6e6f28eea71cae52de0c3bedaa88fa2`).
