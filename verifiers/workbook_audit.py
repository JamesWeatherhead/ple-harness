# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Independent XLSX / CSV / ZIP export auditor for quantity and cost reconciliation."""

from __future__ import annotations

import ast
import io
import operator
import re
import zipfile
from typing import Any

import openpyxl

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


def _col_idx(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _eval_simple_formula(
    formula: str,
    sheet_name: str,
    wb_formulas: openpyxl.Workbook,
    wb_values: openpyxl.Workbook,
    depth: int = 0,
) -> float | None:
    """Evaluates common arithmetic / SUM Excel formulas without executing arbitrary code."""
    if depth > 12 or not isinstance(formula, str) or not formula.startswith("="):
        return None
    expr = formula[1:].strip()

    def get_cell(sname: str, col_str: str, row_num: int) -> float:
        ws_f = wb_formulas[sname]
        ws_v = wb_values[sname]
        c = _col_idx(col_str)
        val = ws_v.cell(row_num, c).value
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
        raw_f = ws_f.cell(row_num, c).value
        if isinstance(raw_f, (int, float)) and not isinstance(raw_f, bool):
            return float(raw_f)
        if isinstance(raw_f, str) and raw_f.startswith("="):
            res = _eval_simple_formula(raw_f, sname, wb_formulas, wb_values, depth + 1)
            if res is not None:
                return res
        raise ValueError(f"Unresolvable cell {sname}!{col_str}{row_num}")

    # Expand SUM(...) first
    def repl_sum(match: re.Match[str]) -> str:
        inner = match.group(1)
        total = 0.0
        for part in inner.split(","):
            part = part.strip()
            rm = _RANGE_REF_RE.fullmatch(part)
            if rm:
                sname = rm.group(1) or rm.group(2) or sheet_name
                c1, r1 = _col_idx(rm.group(3)), int(rm.group(4))
                c2, r2 = _col_idx(rm.group(5)), int(rm.group(6))
                for rr in range(min(r1, r2), max(r1, r2) + 1):
                    for cc in range(min(c1, c2), max(c1, c2) + 1):
                        ws_f = wb_formulas[sname]
                        ws_v = wb_values[sname]
                        vv = ws_v.cell(rr, cc).value
                        if vv is None:
                            rf = ws_f.cell(rr, cc).value
                            if isinstance(rf, (int, float)):
                                vv = rf
                            elif isinstance(rf, str) and rf.startswith("="):
                                vv = _eval_simple_formula(rf, sname, wb_formulas, wb_values, depth + 1)
                        if isinstance(vv, (int, float)):
                            total += float(vv)
            else:
                cm = _CELL_REF_RE.fullmatch(part)
                if not cm:
                    raise ValueError(f"Unsupported SUM arg: {part}")
                sname = cm.group(1) or cm.group(2) or sheet_name
                total += get_cell(sname, cm.group(3), int(cm.group(4)))
        return repr(total)

    try:
        expr = re.sub(r"\bSUM\(([^()]+)\)", repl_sum, expr, flags=re.I)
        expr = _CELL_REF_RE.sub(
            lambda m: repr(get_cell(m.group(1) or m.group(2) or sheet_name, m.group(3), int(m.group(4)))),
            expr,
        )
        node = ast.parse(expr, mode="eval")
    except Exception:
        return None

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

    try:
        return ev(node)
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


def _sheet_rows_as_dicts(ws_f: Any, ws_v: Any, sheet_name: str, wb_f: Any, wb_v: Any) -> list[dict[str, Any]]:
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
                    val = _eval_simple_formula(raw_f, sheet_name, wb_f, wb_v)
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
) -> dict[str, Any]:
    """Audits an XLSX workbook independently for errors, inventory coverage, row sums, and narrative parity."""
    defects: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    wb_f = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    wb_v = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)

    formula_errors: list[dict[str, Any]] = []
    uncached_formulas: list[str] = []
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
                if isinstance(fv, str) and fv.startswith("=") and vv is None:
                    uncached_formulas.append(f"{sname}!R{r}C{c}")

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

    # Check required item IDs in summary/items sheets
    present_items: set[str] = set()
    items_rows: list[dict[str, Any]] = []
    if items_sheet in wb_f.sheetnames:
        items_rows = _sheet_rows_as_dicts(wb_f[items_sheet], wb_v[items_sheet], items_sheet, wb_f, wb_v)
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
        det_rows = _sheet_rows_as_dicts(wb_f[detail_sheet], wb_v[detail_sheet], detail_sheet, wb_f, wb_v)
        sum_rows = _sheet_rows_as_dicts(wb_f[summary_sheet], wb_v[summary_sheet], summary_sheet, wb_f, wb_v)
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
        "uncached_formulas": len(uncached_formulas),
        "rollup_checks": rollup_checks,
        "items_found": sorted(present_items),
        "defects": defects,
        "warnings": warnings,
    }


def audit_zip_deliverables(zip_bytes: bytes, **kwargs: Any) -> dict[str, Any]:
    """Opens a submission.zip in memory, locates XLSX + Markdown narrative, and runs audit_workbook_bytes."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()
        xlsx_names = [n for n in names if n.lower().endswith(".xlsx")]
        md_names = [n for n in names if n.lower().endswith((".md", ".txt"))]
        if not xlsx_names:
            return {
                "ok": False,
                "zip_entries": names,
                "defects": [
                    make_defect(
                        item="missing_xlsx",
                        requirement_source="submission.zip deliverable packaging",
                        measured=names,
                        limit=">= 1 .xlsx workbook",
                        tolerance=0,
                        earliest_suspect_dependency="Workbook generation/export into submission.zip",
                    )
                ],
                "warnings": [],
            }
        narrative = "\n".join(zf.read(m).decode("utf-8", errors="replace") for m in md_names)
        res = audit_workbook_bytes(zf.read(xlsx_names[0]), narrative_text=narrative, **kwargs)
        res["zip_entries"] = names
        res["audited_xlsx"] = xlsx_names[0]
        return res
