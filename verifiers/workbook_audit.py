# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Independent XLSX / CSV / ZIP export auditor for quantity and cost reconciliation."""

from __future__ import annotations

import ast
import io
import math
import operator
import re
import zipfile
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter

ERROR_TOKENS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!", "#NULL!")

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_CELL_REF_RE = re.compile(
    r"(?:(?:'([^']+)'|([A-Za-z0-9_]+))!)?\$?([A-Z]{1,3})\$?(\d+)"
)
_RANGE_REF_RE = re.compile(
    r"(?:(?:'([^']+)'|([A-Za-z0-9_]+))!)?\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)"
)
_FUNC_CALL_RE = re.compile(r"\b(SUM|AVERAGE|MIN|MAX|COUNT|ROUND)\s*\(", re.I)


class _CircularReferenceError(Exception):
    """Internal signal raised when formula evaluation encounters a cycle."""


def _col_idx(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _eval_arith_ast(expr: str) -> float:
    node = ast.parse(expr, mode="eval")

    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY_OPS:
            return _UNARY_OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN_OPS:
            return _BIN_OPS[type(n.op)](ev(n.left), ev(n.right))
        raise ValueError("Unsupported AST node")

    val = ev(node)
    if not math.isfinite(val):
        raise ValueError("Non-finite arithmetic result")
    return val


def _split_top_level_args(arg_str: str) -> list[str]:
    args: list[str] = []
    cur: list[str] = []
    depth = 0
    in_quote = False
    for ch in arg_str:
        if ch == "'":
            in_quote = not in_quote
            cur.append(ch)
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append("".join(cur).strip())
                cur = []
                continue
            cur.append(ch)
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail or args:
        args.append(tail)
    return args


def _find_matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    in_quote = False
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return idx
    raise ValueError("Unbalanced parentheses in formula")


def _eval_simple_formula(
    formula: str,
    sheet_name: str,
    wb_formulas: openpyxl.Workbook,
    wb_values: openpyxl.Workbook,
    depth: int = 0,
    visiting: frozenset[tuple[str, int, int]] = frozenset(),
    circular_refs: list[str] | None = None,
    _verified_cache: dict[tuple[str, int, int], float] | None = None,
) -> float | None:
    """Evaluates arithmetic and SUM/AVERAGE/MIN/MAX/COUNT/ROUND Excel formulas with cycle detection."""
    if depth > 24 or not isinstance(formula, str) or not formula.startswith("="):
        return None
    expr = formula[1:].strip()
    if _verified_cache is None:
        _verified_cache = {}

    def resolve_cell(sname: str, col_idx: int, row_num: int, *, require_value: bool = True) -> float | None:
        cell_key = (sname, row_num, col_idx)
        if cell_key in visiting:
            ref_label = f"{sname}!{get_column_letter(col_idx)}{row_num}"
            if circular_refs is not None and ref_label not in circular_refs:
                circular_refs.append(ref_label)
            raise _CircularReferenceError(ref_label)

        if cell_key in _verified_cache:
            return _verified_cache[cell_key]

        ws_f = wb_formulas[sname]
        ws_v = wb_values[sname]
        val = ws_v.cell(row_num, col_idx).value
        raw_f = ws_f.cell(row_num, col_idx).value

        if isinstance(raw_f, str) and raw_f.startswith("="):
            if circular_refs is not None or not isinstance(val, (int, float)) or isinstance(val, bool):
                res = _eval_simple_formula(
                    raw_f,
                    sname,
                    wb_formulas,
                    wb_values,
                    depth + 1,
                    visiting | {cell_key},
                    circular_refs,
                    _verified_cache,
                )
                if res is not None:
                    _verified_cache[cell_key] = res
                    return res

        if isinstance(val, (int, float)) and not isinstance(val, bool):
            fval = float(val)
            _verified_cache[cell_key] = fval
            return fval
        if isinstance(raw_f, (int, float)) and not isinstance(raw_f, bool):
            fval = float(raw_f)
            _verified_cache[cell_key] = fval
            return fval
        if require_value:
            raise ValueError(f"Unresolvable cell {sname}!{get_column_letter(col_idx)}{row_num}")
        return None

    def get_cell(sname: str, col_str: str, row_num: int) -> float:
        res = resolve_cell(sname, _col_idx(col_str), row_num, require_value=True)
        assert res is not None
        return res

    def collect_arg_values(parts: list[str]) -> list[float]:
        values: list[float] = []
        for part in parts:
            if not part:
                continue
            rm = _RANGE_REF_RE.fullmatch(part)
            if rm:
                sname = rm.group(1) or rm.group(2) or sheet_name
                c1, r1 = _col_idx(rm.group(3)), int(rm.group(4))
                c2, r2 = _col_idx(rm.group(5)), int(rm.group(6))
                for rr in range(min(r1, r2), max(r1, r2) + 1):
                    for cc in range(min(c1, c2), max(c1, c2) + 1):
                        vv = resolve_cell(sname, cc, rr, require_value=False)
                        if vv is not None:
                            values.append(vv)
                continue
            cm = _CELL_REF_RE.fullmatch(part)
            if cm:
                sname = cm.group(1) or cm.group(2) or sheet_name
                vv = resolve_cell(sname, _col_idx(cm.group(3)), int(cm.group(4)), require_value=False)
                if vv is not None:
                    values.append(vv)
                continue
            sub = _CELL_REF_RE.sub(
                lambda m: repr(get_cell(m.group(1) or m.group(2) or sheet_name, m.group(3), int(m.group(4)))),
                part,
            )
            values.append(_eval_arith_ast(sub))
        return values

    def eval_func_call(func_name: str, arg_str: str) -> float:
        parts = _split_top_level_args(arg_str)
        fname = func_name.upper()
        if fname == "ROUND":
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError(f"ROUND expects 2 arguments, got {parts!r}")
            x_expr = _CELL_REF_RE.sub(
                lambda m: repr(get_cell(m.group(1) or m.group(2) or sheet_name, m.group(3), int(m.group(4)))),
                parts[0],
            )
            n_expr = _CELL_REF_RE.sub(
                lambda m: repr(get_cell(m.group(1) or m.group(2) or sheet_name, m.group(3), int(m.group(4)))),
                parts[1],
            )
            x_val = _eval_arith_ast(x_expr)
            n_val = int(round(_eval_arith_ast(n_expr)))
            return float(round(x_val, n_val))

        values = collect_arg_values(parts)
        if fname == "SUM":
            return float(sum(values))
        if fname == "COUNT":
            return float(len(values))
        if fname == "AVERAGE":
            if not values:
                raise ValueError("AVERAGE over empty values")
            return float(sum(values) / len(values))
        if fname == "MIN":
            return float(min(values)) if values else 0.0
        if fname == "MAX":
            return float(max(values)) if values else 0.0
        raise ValueError(f"Unsupported function: {func_name}")

    try:
        while True:
            matches = list(_FUNC_CALL_RE.finditer(expr))
            if not matches:
                break
            m = matches[-1]
            open_paren_idx = m.end() - 1
            close_paren_idx = _find_matching_paren(expr, open_paren_idx)
            func_val = eval_func_call(m.group(1), expr[open_paren_idx + 1 : close_paren_idx])
            expr = expr[: m.start()] + repr(func_val) + expr[close_paren_idx + 1 :]

        expr = _CELL_REF_RE.sub(
            lambda m: repr(get_cell(m.group(1) or m.group(2) or sheet_name, m.group(3), int(m.group(4)))),
            expr,
        )
        return _eval_arith_ast(expr)
    except _CircularReferenceError:
        if depth > 0:
            raise
        return None
    except Exception:
        return None


def make_defect(
    item: str,
    requirement_source: str,
    measured: Any,
    limit: Any,
    tolerance: Any,
    earliest_suspect_dependency: str,
    severity: str = "hard",
) -> dict[str, Any]:
    return {
        "item": item,
        "requirement_source": requirement_source,
        "measured": measured,
        "limit": limit,
        "tolerance": tolerance,
        "earliest_suspect_dependency": earliest_suspect_dependency,
        "severity": severity,
    }


def _sheet_rows_as_dicts(
    ws_f: Any,
    ws_v: Any,
    sheet_name: str,
    wb_f: Any,
    wb_v: Any,
    *,
    verified_cache: dict[tuple[str, int, int], float] | None = None,
) -> list[dict[str, Any]]:
    if ws_f.max_row < 2:
        return []
    headers = [str(ws_v.cell(1, c).value or f"col_{c}").strip() for c in range(1, ws_f.max_column + 1)]
    out: list[dict[str, Any]] = []
    for r in range(2, ws_f.max_row + 1):
        row_d: dict[str, Any] = {"_row": r}
        non_empty = False
        for c, h in enumerate(headers, 1):
            val = ws_v.cell(r, c).value
            if val is None:
                raw_f = ws_f.cell(r, c).value
                if isinstance(raw_f, str) and raw_f.startswith("="):
                    val = _eval_simple_formula(
                        raw_f,
                        sheet_name,
                        wb_f,
                        wb_v,
                        visiting=frozenset({(sheet_name, r, c)}),
                        _verified_cache=verified_cache,
                    )
                else:
                    val = raw_f
            if val is not None:
                non_empty = True
            row_d[h] = val
        if non_empty:
            out.append(row_d)
    return out


def audit_workbook_bytes(
    xlsx_bytes: bytes,
    *,
    required_items: list[str] | None = None,
    narrative_text: str = "",
    detail_sheet: str = "Takeoff",
    summary_sheet: str = "Alternatives",
    items_sheet: str = "Items",
    group_cols: tuple[str, ...] = ("item_id", "type"),
    sum_cols: tuple[str, ...] = ("labor_mh", "labor_usd", "equipment_usd", "material_usd", "subcontract_usd"),
    rollup_tolerance: float = 0.05,
    require_cached_formulas: bool = False,
    template_schemas: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """Audits an XLSX workbook independently for errors, inventory coverage, row sums, and narrative parity."""
    defects: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    wb_f = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    wb_v = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)

    formula_errors: list[dict[str, Any]] = []
    uncached_formulas: list[str] = []
    circular_refs: list[str] = []
    verified_cache: dict[tuple[str, int, int], float] = {}
    for sname in wb_f.sheetnames:
        ws_f = wb_f[sname]
        ws_v = wb_v[sname]
        for r in range(1, ws_f.max_row + 1):
            for c in range(1, ws_f.max_column + 1):
                fv = ws_f.cell(r, c).value
                vv = ws_v.cell(r, c).value
                for check_val in (fv, vv):
                    if isinstance(check_val, str) and any(tok in check_val for tok in ERROR_TOKENS):
                        formula_errors.append({"sheet": sname, "row": r, "col": c, "value": check_val})
                if isinstance(fv, str) and fv.startswith("="):
                    if vv is None:
                        uncached_formulas.append(f"{sname}!R{r}C{c}")
                    _eval_simple_formula(
                        fv,
                        sname,
                        wb_f,
                        wb_v,
                        depth=0,
                        visiting=frozenset({(sname, r, c)}),
                        circular_refs=circular_refs,
                        _verified_cache=verified_cache,
                    )

    if formula_errors:
        defects.append(
            make_defect(
                item="workbook_formula_errors",
                requirement_source="XLSX integrity check",
                measured=len(formula_errors),
                limit=0,
                tolerance=0,
                earliest_suspect_dependency=f"{formula_errors[0]['sheet']} row {formula_errors[0]['row']}: {formula_errors[0]['value']}",
            )
        )

    if circular_refs:
        defects.append(
            make_defect(
                item="workbook_circular_references",
                requirement_source="XLSX formula acyclicity check",
                measured=circular_refs,
                limit=0,
                tolerance=0,
                earliest_suspect_dependency=f"Circular reference involving {circular_refs[0]}",
            )
        )

    if uncached_formulas:
        target_list = defects if require_cached_formulas else warnings
        target_list.append(
            make_defect(
                item="uncached_formula_cells",
                requirement_source="XLSX cached evaluation check (data_only=True)",
                measured=len(uncached_formulas),
                limit=0,
                tolerance=0,
                earliest_suspect_dependency=f"First uncached cell: {uncached_formulas[0]}",
                severity="hard" if require_cached_formulas else "warning",
            )
        )

    if template_schemas:
        cand_sheets = set(wb_f.sheetnames)
        best_tmpl_name: str | None = None
        best_tmpl_sheets: dict[str, list[str]] | None = None
        best_score = (0, 0.0)
        for tmpl_name, tmpl_sheets in template_schemas.items():
            if not tmpl_sheets:
                continue
            overlap = len(cand_sheets & set(tmpl_sheets.keys()))
            min_req = 1 if len(tmpl_sheets) == 1 else 2
            if overlap >= min_req:
                ratio = overlap / max(1, len(tmpl_sheets))
                score = (overlap, ratio)
                if score > best_score:
                    best_score = score
                    best_tmpl_name = tmpl_name
                    best_tmpl_sheets = tmpl_sheets
        if best_tmpl_name and best_tmpl_sheets:
            for req_sheet, req_cols in best_tmpl_sheets.items():
                if req_sheet not in wb_f.sheetnames:
                    defects.append(
                        make_defect(
                            item=f"template_missing_sheet:{req_sheet}",
                            requirement_source=f"Starter template '{best_tmpl_name}' sheet structure",
                            measured=wb_f.sheetnames,
                            limit=req_sheet,
                            tolerance=0,
                            earliest_suspect_dependency=f"Clone '{best_tmpl_name}' directly preserving sheet '{req_sheet}'",
                            severity="blocker",
                        )
                    )
                    continue
                if not req_cols:
                    continue
                ws_t = wb_f[req_sheet]
                ws_tv = wb_v[req_sheet]
                seen_headers: set[str] = set()
                for rr in range(1, min(6, ws_t.max_row + 1)):
                    for cc in range(1, ws_t.max_column + 1):
                        for raw_val in (ws_t.cell(rr, cc).value, ws_tv.cell(rr, cc).value):
                            if raw_val is not None:
                                s_val = str(raw_val).strip()
                                if s_val:
                                    seen_headers.add(s_val)
                missing_cols = [col for col in req_cols if col not in seen_headers]
                if missing_cols:
                    defects.append(
                        make_defect(
                            item=f"template_missing_columns:{req_sheet}",
                            requirement_source=f"Starter template '{best_tmpl_name}' column contract",
                            measured=f"missing {missing_cols}",
                            limit=req_cols,
                            tolerance=0,
                            earliest_suspect_dependency=(
                                f"Restore missing columns {missing_cols} in sheet '{req_sheet}' by modifying "
                                f"a copy of '{best_tmpl_name}' in-place"
                            ),
                            severity="blocker",
                        )
                    )

    # Check required item IDs in summary/items sheets
    present_items: set[str] = set()
    items_rows: list[dict[str, Any]] = []
    if items_sheet in wb_f.sheetnames:
        items_rows = _sheet_rows_as_dicts(
            wb_f[items_sheet], wb_v[items_sheet], items_sheet, wb_f, wb_v, verified_cache=verified_cache
        )
        for row in items_rows:
            if row.get("item_id"):
                present_items.add(str(row["item_id"]).strip())

    if required_items:
        missing = sorted(set(required_items) - present_items)
        if missing:
            defects.append(
                make_defect(
                    item="missing_required_items",
                    requirement_source=f"Required inventory vs {items_sheet} sheet",
                    measured=sorted(present_items),
                    limit=sorted(required_items),
                    tolerance="exact set match",
                    earliest_suspect_dependency=f"Source inventory acquisition for missing items: {missing}",
                )
            )

    # Independent detail -> summary rollup check
    rollup_checks = 0
    if detail_sheet in wb_f.sheetnames and summary_sheet in wb_f.sheetnames:
        det_rows = _sheet_rows_as_dicts(
            wb_f[detail_sheet], wb_v[detail_sheet], detail_sheet, wb_f, wb_v, verified_cache=verified_cache
        )
        sum_rows = _sheet_rows_as_dicts(
            wb_f[summary_sheet], wb_v[summary_sheet], summary_sheet, wb_f, wb_v, verified_cache=verified_cache
        )
        grouped: dict[tuple[str, ...], dict[str, float]] = {}
        for row in det_rows:
            key = tuple(str(row.get(gc) or "").strip() for gc in group_cols)
            bucket = grouped.setdefault(key, {sc: 0.0 for sc in sum_cols})
            for sc in sum_cols:
                v = row.get(sc)
                if isinstance(v, (int, float)):
                    bucket[sc] += float(v)

        for srow in sum_rows:
            key = tuple(str(srow.get(gc) or "").strip() for gc in group_cols)
            if not any(key):
                continue
            rollup_checks += 1
            exp = grouped.get(key)
            if exp is None:
                defects.append(
                    make_defect(
                        item="|".join(key),
                        requirement_source=f"{detail_sheet} -> {summary_sheet} rollup",
                        measured="0 detail rows",
                        limit=">= 1 detail row",
                        tolerance=0,
                        earliest_suspect_dependency=f"{detail_sheet} missing rows for {key}",
                    )
                )
                continue
            for sc in sum_cols:
                if sc in srow and isinstance(srow[sc], (int, float)):
                    got = float(srow[sc])
                    want = exp[sc]
                    if abs(got - want) > rollup_tolerance:
                        defects.append(
                            make_defect(
                                item=f"{'|'.join(key)}:{sc}",
                                requirement_source=f"{detail_sheet} sum -> {summary_sheet}.{sc}",
                                measured=round(got, 4),
                                limit=round(want, 4),
                                tolerance=rollup_tolerance,
                                earliest_suspect_dependency=f"{detail_sheet} rows for {key} column {sc}",
                            )
                        )

        # Reconcile recommended Alternatives rows against Items sheet
        if items_rows:
            rec_by_item = {
                str(r.get("item_id") or "").strip(): r
                for r in sum_rows
                if str(r.get("recommended") or "").strip().upper() in ("Y", "YES", "TRUE", "1")
            }
            for irow in items_rows:
                iid = str(irow.get("item_id") or "").strip()
                arow = rec_by_item.get(iid)
                if arow is None:
                    defects.append(
                        make_defect(
                            item=f"{iid}:recommended_match",
                            requirement_source=f"{items_sheet} vs {summary_sheet} recommended='Y'",
                            measured="none",
                            limit="1 recommended alternative",
                            tolerance=0,
                            earliest_suspect_dependency=f"{summary_sheet} recommended flag for {iid}",
                        )
                    )
                    continue
                for col in ("total_project_cost_usd", "route_length_ft"):
                    if col in irow and col in arow and isinstance(irow[col], (int, float)) and isinstance(arow[col], (int, float)):
                        # Allow up to $1.00 or 0.15 ft rounding if formulas rounded
                        tol = 1.0 if "usd" in col else 0.15
                        if abs(float(irow[col]) - float(arow[col])) > tol:
                            defects.append(
                                make_defect(
                                    item=f"{iid}:{col}",
                                    requirement_source=f"{items_sheet}.{col} vs {summary_sheet}.{col}",
                                    measured=float(irow[col]),
                                    limit=float(arow[col]),
                                    tolerance=tol,
                                    earliest_suspect_dependency=f"{items_sheet} row {irow['_row']} {col}",
                                )
                            )

    # Narrative vs workbook reconciliation
    if narrative_text and items_rows:
        for irow in items_rows:
            iid = str(irow.get("item_id") or "").strip()
            cost = irow.get("total_project_cost_usd")
            if iid and isinstance(cost, (int, float)):
                # Check if item_id appears and its rounded integer dollar figure or exact figure appears nearby
                rounded_int = f"{round(float(cost)):,}"
                exact_str = f"{float(cost):,.2f}"
                plain_int = str(round(float(cost)))
                if iid in narrative_text:
                    # Find snippet around item_id (up to 400 chars)
                    idx = narrative_text.find(iid)
                    window = narrative_text[max(0, idx - 80) : idx + 500]
                    if not (rounded_int in window or exact_str in window or plain_int in window):
                        # Also check if any dollar figure appears in window and differs
                        dollars = re.findall(r"\$([0-9][0-9,]*(?:\.\d+)?)", window)
                        if dollars:
                            first_val = float(dollars[0].replace(",", ""))
                            if abs(first_val - float(cost)) > 2.0:
                                defects.append(
                                    make_defect(
                                        item=f"{iid}:narrative_cost_reconciliation",
                                        requirement_source=f"Narrative report vs {items_sheet}.total_project_cost_usd",
                                        measured=first_val,
                                        limit=round(float(cost), 2),
                                        tolerance=2.0,
                                        earliest_suspect_dependency=f"Narrative text for {iid} vs {items_sheet}",
                                    )
                                )

    return {
        "ok": len(defects) == 0,
        "sheets": wb_f.sheetnames,
        "formula_errors": len(formula_errors),
        "circular_references": len(circular_refs),
        "uncached_formulas": len(uncached_formulas),
        "rollup_checks": rollup_checks,
        "items_found": sorted(present_items),
        "defects": defects,
        "warnings": warnings,
    }


_ZIP_PLACEHOLDER_RE = re.compile(
    r"(?:\b(?:TODO|TBD|FIXME|XXX)\b|\[INSERT\b|iteration-1\s+provisional)",
    re.I,
)
_NEGATED_PLACEHOLDER_RE = re.compile(
    r"\b(?:no|zero|none|without|resolved|replaced|cleared)\b[^\n.;]{0,30}(?:\b(?:TBD|TODO|FIXME|XXX|placeholders?)\b|provisional)",
    re.I,
)


def _find_placeholder_hits(text: str, source_name: str, defects: list[dict[str, Any]]) -> None:
    """Appends a defect if unnegated unfinished placeholder/provisional markers appear."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _NEGATED_PLACEHOLDER_RE.search(line):
            continue
        m = _ZIP_PLACEHOLDER_RE.search(line)
        if m:
            defects.append(
                make_defect(
                    item=f"placeholder_marker:{source_name}",
                    requirement_source="Deliverable finality check",
                    measured=f"line {lineno}: {m.group(0)}",
                    limit="0 unfinished placeholders",
                    tolerance=0,
                    earliest_suspect_dependency=f"Replace placeholder/provisional text in {source_name}",
                )
            )
            return


def _scan_xlsx_placeholders(xlsx_bytes: bytes, source_name: str, defects: list[dict[str, Any]]) -> None:
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    for sname in wb.sheetnames:
        ws = wb[sname]
        for r in range(1, ws.max_row + 1):
            for c in range(1, ws.max_column + 1):
                val = ws.cell(r, c).value
                if isinstance(val, str) and not val.startswith("="):
                    if _NEGATED_PLACEHOLDER_RE.search(val):
                        continue
                    m = _ZIP_PLACEHOLDER_RE.search(val)
                    if m:
                        defects.append(
                            make_defect(
                                item=f"placeholder_marker:{source_name}",
                                requirement_source="Deliverable finality check",
                                measured=f"{sname}!R{r}C{c}: {m.group(0)}",
                                limit="0 unfinished placeholders",
                                tolerance=0,
                                earliest_suspect_dependency=f"Replace placeholder text in {source_name} ({sname})",
                            )
                        )
                        return


def audit_zip_deliverables(
    zip_bytes: bytes,
    *,
    require_xlsx: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Opens a submission zip in memory and recursively audits all XLSX, DXF, PDF, IFC, and narrative members."""
    from verifiers.cad_pdf_audit import audit_dxf_bytes, audit_pdf_bytes
    from verifiers.geometry_ifc_audit import audit_ifc4_bytes

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        corrupt = zf.testzip()
        if corrupt is not None:
            return {
                "ok": False,
                "zip_entries": zf.namelist(),
                "defects": [
                    make_defect(
                        item=f"zip_corrupt_member:{corrupt}",
                        requirement_source="submission.zip CRC integrity check",
                        measured=corrupt,
                        limit="valid CRC-32 across all members",
                        tolerance=0,
                        earliest_suspect_dependency=f"Archive packaging of {corrupt}",
                    )
                ],
                "warnings": [],
            }

        infos = [info for info in zf.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        if not names:
            return {
                "ok": False,
                "zip_entries": [],
                "defects": [
                    make_defect(
                        item="zip_empty",
                        requirement_source="submission.zip deliverable packaging",
                        measured=0,
                        limit=">= 1 deliverable file",
                        tolerance=0,
                        earliest_suspect_dependency="Populate submission.zip with requested deliverables",
                    )
                ],
                "warnings": [],
            }

        defects: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for info in infos:
            if info.file_size == 0:
                defects.append(
                    make_defect(
                        item=f"empty_zip_member:{info.filename}",
                        requirement_source="submission.zip non-empty member check",
                        measured=0,
                        limit="> 0 bytes",
                        tolerance=0,
                        earliest_suspect_dependency=f"Write non-empty deliverable content to {info.filename}",
                    )
                )

        xlsx_names = [n for n in names if n.lower().endswith((".xlsx", ".xlsm"))]
        md_names = [n for n in names if n.lower().endswith((".md", ".txt"))]
        dxf_names = [n for n in names if n.lower().endswith(".dxf")]
        pdf_names = [n for n in names if n.lower().endswith(".pdf")]
        ifc_names = [n for n in names if n.lower().endswith(".ifc")]

        narrative_parts: list[str] = []
        for m in md_names:
            text = zf.read(m).decode("utf-8", errors="replace")
            narrative_parts.append(text)
            _find_placeholder_hits(text, m, defects)
        narrative = "\n".join(narrative_parts)

        primary_res: dict[str, Any] = {}
        audited_members: list[str] = []
        if xlsx_names:
            for idx, xname in enumerate(xlsx_names):
                xbytes = zf.read(xname)
                xres = audit_workbook_bytes(xbytes, narrative_text=narrative, **kwargs)
                _scan_xlsx_placeholders(xbytes, xname, defects)
                if idx == 0:
                    primary_res = dict(xres)
                else:
                    defects.extend(xres.get("defects") or [])
                    warnings.extend(xres.get("warnings") or [])
                audited_members.append(xname)
        elif require_xlsx:
            defects.append(
                make_defect(
                    item="missing_xlsx",
                    requirement_source="submission.zip deliverable packaging",
                    measured=names,
                    limit=">= 1 .xlsx workbook",
                    tolerance=0,
                    earliest_suspect_dependency="Workbook generation/export into submission.zip",
                )
            )

        for dname in dxf_names:
            dres = audit_dxf_bytes(zf.read(dname))
            defects.extend(dres.get("defects") or [])
            warnings.extend(dres.get("warnings") or [])
            audited_members.append(dname)

        for pname in pdf_names:
            pres = audit_pdf_bytes(zf.read(pname))
            defects.extend(pres.get("defects") or [])
            warnings.extend(pres.get("warnings") or [])
            audited_members.append(pname)

        for iname in ifc_names:
            ires = audit_ifc4_bytes(zf.read(iname))
            defects.extend(ires.defects or [])
            audited_members.append(iname)

        merged_defects = (primary_res.get("defects") or []) + defects
        merged_warnings = (primary_res.get("warnings") or []) + warnings
        out = dict(primary_res)
        out["ok"] = len(merged_defects) == 0
        out["defects"] = merged_defects
        out["warnings"] = merged_warnings
        out["zip_entries"] = names
        out["audited_members"] = audited_members
        if xlsx_names:
            out["audited_xlsx"] = xlsx_names[0]
            out["audited_xlsx_list"] = xlsx_names
        return out
