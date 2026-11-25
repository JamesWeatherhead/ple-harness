"""Synthetic 7-circuit MV conveyance workbook audit, failure injection, and incumbent retention tests."""

from __future__ import annotations

import asyncio
import io
import xml.etree.ElementTree as ET
import zipfile

import openpyxl

from harness import EngineeringCoordinator, RuntimeConfig
from verifiers import audit_workbook_bytes, audit_zip_deliverables


def _inject_cached_formula_values(xlsx_bytes: bytes, cached: dict[tuple[str, str], float]) -> bytes:
    """Injects cached numeric <v> values alongside Excel formulas in XLSX XML so data_only=True reads them."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes), "r") as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        wb_xml = ET.fromstring(zin.read("xl/workbook.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        rels_xml = ET.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {rel.attrib["Id"]: rel.attrib["Target"].lstrip("/") for rel in rels_xml}
        sheet_to_path: dict[str, str] = {}
        for sh in wb_xml.findall(".//s:sheet", ns):
            rid = sh.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            tgt = rid_to_target[rid]
            if not tgt.startswith("xl/"):
                tgt = f"xl/{tgt}"
            sheet_to_path[tgt] = sh.attrib["name"]

        ET.register_namespace("", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
        for info in zin.infolist():
            data = zin.read(info.filename)
            sname = sheet_to_path.get(info.filename)
            if sname:
                root = ET.fromstring(data)
                for cell in root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                    coord = cell.attrib.get("r", "")
                    key = (sname, coord)
                    if key in cached:
                        cell.set("t", "n")
                        v_el = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                        if v_el is None:
                            v_el = ET.SubElement(cell, "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                        v_el.text = repr(cached[key])
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            zout.writestr(info, data)
    return out.getvalue()


def _build_mv_bundle(
    *,
    drop_c07: bool = False,
    formula_error: bool = False,
    leave_uncached: bool = False,
    skew_alt_material: float = 0.0,
    skew_narrative_c01: bool = False,
) -> tuple[bytes, str, bytes]:
    wb = openpyxl.Workbook()
    ws_takeoff = wb.active
    ws_takeoff.title = "Takeoff"
    ws_alt = wb.create_sheet("Alternatives")
    ws_items = wb.create_sheet("Items")

    ws_takeoff.append(["item_id", "type", "code", "quantity", "unit", "labor_mh", "labor_usd", "equipment_usd", "material_usd", "subcontract_usd"])
    ws_alt.append(["item_id", "type", "recommended", "route_length_ft", "labor_mh", "labor_usd", "equipment_usd", "material_usd", "subcontract_usd", "direct_cost_usd", "total_project_cost_usd"])
    ws_items.append(["item_id", "recommended_type", "route_length_ft", "total_project_cost_usd"])

    circuits = [
        ("C01", "CEDB", 834.1, 100.0, 20000.0, 5000.0, 40000.0, 10000.0),
        ("C02", "CEDB", 1103.4, 120.0, 24000.0, 6000.0, 45000.0, 10000.0),
        ("C03", "CEDB", 344.9, 60.0, 12000.0, 2000.0, 20000.0, 0.0),
        ("C04", "CEDB", 1199.2, 110.0, 22000.0, 4000.0, 50000.0, 12000.0),
        ("C05", "RMC", 400.0, 80.0, 16000.0, 2500.0, 35000.0, 0.0),
        ("C06", "OH", 1838.2, 95.0, 19000.0, 3500.0, 28000.0, 0.0),
        ("C07", "CEDB", 680.0, 75.0, 15000.0, 3000.0, 32000.0, 5000.0),
    ]

    cached: dict[tuple[str, str], float] = {}
    t_row = 2
    a_row = 2
    i_row = 2
    narr_lines = ["# Conveyance Study\n"]
    markup_mult = 1.25

    for cid, ctype, length_ft, mh, lab, eq, mat, sub in circuits:
        # Two takeoff rows summing cleanly to circuit total
        for frac in (0.4, 0.6):
            ws_takeoff.append([
                cid, ctype, f"CODE-{cid}", length_ft * frac, "LF",
                round(mh * frac, 4), round(lab * frac, 2), round(eq * frac, 2),
                round(mat * frac, 2), round(sub * frac, 2),
            ])
            t_row += 1

        alt_mat = mat + (skew_alt_material if cid == "C01" else 0.0)
        direct_val = lab + eq + alt_mat + sub
        tpc_val = round(direct_val * markup_mult, 2)
        direct_formula = f"=F{a_row}+G{a_row}+H{a_row}+I{a_row}" if not (formula_error and cid == "C02") else "=#REF!"
        tpc_formula = f"=J{a_row}*{markup_mult}"

        ws_alt.append([cid, ctype, "Y", length_ft, mh, lab, eq, alt_mat, sub, direct_formula, tpc_formula])
        cached[("Alternatives", f"J{a_row}")] = direct_val
        cached[("Alternatives", f"K{a_row}")] = tpc_val

        if not (drop_c07 and cid == "C07"):
            ws_items.append([cid, ctype, length_ft, f"=Alternatives!K{a_row}"])
            cached[("Items", f"D{i_row}")] = tpc_val
            i_row += 1

        reported_cost = tpc_val + 80000.0 if (skew_narrative_c01 and cid == "C01") else tpc_val
        narr_lines.append(f"- {cid} ({ctype}, {length_ft} ft): TPC ${reported_cost:,.2f}\n")
        a_row += 1

    raw_buf = io.BytesIO()
    wb.save(raw_buf)
    xlsx_bytes = raw_buf.getvalue()
    if not leave_uncached:
        xlsx_bytes = _inject_cached_formula_values(xlsx_bytes, cached)
    narrative = "".join(narr_lines)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conveyance_study.xlsx", xlsx_bytes)
        zf.writestr("conveyance_study_report.md", narrative.encode("utf-8"))
    return xlsx_bytes, narrative, zip_buf.getvalue()


class _InMemorySession:
    def __init__(self, workdir: str = "/workspace") -> None:
        self.workdir = workdir
        self.instruction = "Synthetic MV conveyance test"
        self.limits: dict[str, int] = {"agent_timeout_sec": 600}
        self.mcp_servers: list[dict] = []
        self.final_message = ""
        self.files: dict[str, bytes] = {}
        self.logs: list[str] = []

    def time_left(self) -> float:
        return 600.0

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes | str, append: bool = False) -> int:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if append and path in self.files:
            raw = self.files[path] + raw
        self.files[path] = raw
        return len(raw)

    async def log(self, text: str) -> None:
        self.logs.append(text)

    async def usage(self, *args) -> None:
        pass


def test_clean_mv_workbook_and_zip_pass() -> None:
    xlsx_bytes, narrative, zip_bytes = _build_mv_bundle()
    items = [f"C0{i}" for i in range(1, 8)]
    res = audit_workbook_bytes(
        xlsx_bytes,
        required_items=items,
        narrative_text=narrative,
        require_cached_formulas=True,
    )
    assert res["ok"], res
    assert res["formula_errors"] == 0
    assert res["uncached_formulas"] == 0
    assert res["rollup_checks"] == 7
    assert res["items_found"] == items
    assert res["defects"] == []

    zres = audit_zip_deliverables(zip_bytes, required_items=items, require_cached_formulas=True)
    assert zres["ok"], zres
    assert zres["audited_xlsx"] == "conveyance_study.xlsx"


def test_injected_failures_detected_with_suspect_dependency() -> None:
    items = [f"C0{i}" for i in range(1, 8)]

    # 1. Missing circuit C07
    x1, n1, _ = _build_mv_bundle(drop_c07=True)
    r1 = audit_workbook_bytes(x1, required_items=items, narrative_text=n1, require_cached_formulas=True)
    assert not r1["ok"]
    assert any(d["item"] == "missing_required_items" and "C07" in d["earliest_suspect_dependency"] for d in r1["defects"])

    # 2. Formula error #REF!
    x2, n2, _ = _build_mv_bundle(formula_error=True)
    r2 = audit_workbook_bytes(x2, required_items=items, narrative_text=n2)
    assert not r2["ok"]
    assert any(d["item"] == "workbook_formula_errors" and "#REF!" in d["earliest_suspect_dependency"] for d in r2["defects"])

    # 3. Uncached formula cells under require_cached_formulas=True
    x3, n3, _ = _build_mv_bundle(leave_uncached=True)
    r3 = audit_workbook_bytes(x3, required_items=items, narrative_text=n3, require_cached_formulas=True)
    assert not r3["ok"]
    assert r3["uncached_formulas"] > 0
    assert any(d["item"] == "uncached_formula_cells" for d in r3["defects"])

    # 4. Takeoff -> Alternatives rollup mismatch
    x4, n4, _ = _build_mv_bundle(skew_alt_material=50.0)
    r4 = audit_workbook_bytes(x4, required_items=items, narrative_text=n4, require_cached_formulas=True)
    assert not r4["ok"]
    assert any(d["item"] == "C01|CEDB:material_usd" for d in r4["defects"])

    # 5. Narrative vs Items reconciliation mismatch
    x5, n5, _ = _build_mv_bundle(skew_narrative_c01=True)
    r5 = audit_workbook_bytes(x5, required_items=items, narrative_text=n5, require_cached_formulas=True)
    assert not r5["ok"]
    assert any(d["item"] == "C01:narrative_cost_reconciliation" for d in r5["defects"])


def test_repair_and_incumbent_rollback() -> None:
    async def scenario() -> None:
        session = _InMemorySession()
        coord = EngineeringCoordinator(session, config=RuntimeConfig.from_env())
        path = "/workspace/output/conveyance_study.xlsx"
        clean_x, clean_n, _ = _build_mv_bundle()
        bad_x, bad_n, _ = _build_mv_bundle(skew_alt_material=120.0)

        await session.write_file(path, clean_x)
        r_ok = await coord._run_audit_internal(path, narrative_text=clean_n, require_cached_formulas=True)
        assert r_ok["ok"], r_ok
        await coord._tool_save_incumbent(path, note="clean baseline")

        # Simulate destructive edit overwriting workbook
        await session.write_file(path, bad_x)
        r_bad = await coord._run_audit_internal(path, narrative_text=bad_n, require_cached_formulas=True)
        assert not r_bad["ok"]

        # Roll back via incumbent restoration
        msg = await coord._tool_restore_incumbent(path)
        assert "restored" in msg
        r_restored = await coord._run_audit_internal(path, narrative_text=clean_n, require_cached_formulas=True)
        assert r_restored["ok"], r_restored

    asyncio.run(scenario())
