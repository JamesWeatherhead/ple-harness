# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Synthetic cold-read CAD (.dxf) and drawing/specification (.pdf) audit tests."""

from __future__ import annotations

import asyncio
import io
import math

import ezdxf
import pypdf
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from harness import EngineeringCoordinator, RuntimeConfig
from verifiers import audit_dxf_bytes, audit_pdf_bytes


def _dxf_bytes(doc: ezdxf.document.Drawing, encoding: str = "utf-8") -> bytes:
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode(encoding)


def _make_text_pdf(pages_lines: list[list[str]]) -> bytes:
    writer = pypdf.PdfWriter()
    font_dict = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font_dict)
    for lines in pages_lines:
        page = writer.add_blank_page(width=612, height=792)
        ops = ["BT", "/F1 12 Tf", "72 720 Td", "16 TL"]
        for line in lines:
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj T*")
        ops.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1"))
        stream_ref = writer._add_object(stream)
        page[NameObject("/Contents")] = stream_ref
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_blank_pdf(num_pages: int = 1) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_image_only_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    img = DecodedStreamObject()
    img.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    img.set_data(b"\xff")
    img_ref = writer._add_object(img)
    stream = DecodedStreamObject()
    stream.set_data(b"q 100 0 0 100 72 600 cm /Im1 Do Q")
    stream_ref = writer._add_object(stream)
    page[NameObject("/Contents")] = stream_ref
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): img_ref})}
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_clean_dxf_audit_passes() -> None:
    doc = ezdxf.new("R2010")
    doc.layers.add("M-DUCT-SUPP")
    doc.layers.add("A-ANNO-NOTE")
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (24.0, 0.0), dxfattribs={"layer": "M-DUCT-SUPP"})
    msp.add_lwpolyline([(0.0, 0.0), (24.0, 0.0), (24.0, 12.0), (0.0, 12.0)], dxfattribs={"layer": "M-DUCT-SUPP"})
    msp.add_circle((12.0, 6.0), radius=1.5, dxfattribs={"layer": "M-DUCT-SUPP"})
    txt = msp.add_text("AHU-01 4500 CFM", dxfattribs={"layer": "A-ANNO-NOTE", "height": 0.5})
    txt.set_placement((2.0, 2.0))
    msp.add_mtext(r"{\fArial|b1;STATIC PRESSURE 2.5 IN. W.G.}", dxfattribs={"layer": "A-ANNO-NOTE"})

    res = audit_dxf_bytes(
        _dxf_bytes(doc),
        required_layers=["0", "M-DUCT-SUPP", "A-ANNO-NOTE"],
        required_text=["AHU-01", "4500 CFM", "STATIC PRESSURE 2.5"],
    )
    assert res["ok"], res
    assert res["entity_count"] == 5
    assert "M-DUCT-SUPP" in res["layers"]
    assert "A-ANNO-NOTE" in res["layers"]
    assert res["bbox"] is not None and len(res["bbox"]) == 6
    assert res["bbox"][0] <= 0.0 and res["bbox"][3] >= 24.0
    assert res["defects"] == []


def test_dxf_latin1_and_defects_detected() -> None:
    # 1. Corrupt / empty DXF
    bad_parse = audit_dxf_bytes(b"NOT A DXF FILE")
    assert not bad_parse["ok"]
    assert any(d["item"] == "dxf_parse" for d in bad_parse["defects"])

    empty_parse = audit_dxf_bytes(b"")
    assert not empty_parse["ok"]
    assert any(d["item"] == "dxf_parse" for d in empty_parse["defects"])

    # 2. Empty modelspace
    empty_doc = ezdxf.new("R2010")
    r_empty = audit_dxf_bytes(_dxf_bytes(empty_doc))
    assert not r_empty["ok"]
    assert r_empty["entity_count"] == 0
    assert any(d["item"] == "dxf_modelspace_empty" for d in r_empty["defects"])

    # 3. Missing required layer & required text (plus latin-1 tolerance)
    doc_miss = ezdxf.new("R2000")
    doc_miss.layers.add("E-POWR")
    msp = doc_miss.modelspace()
    msp.add_line((0.0, 0.0), (10.0, 0.0), dxfattribs={"layer": "E-POWR"})
    t = msp.add_text("TEMP 25°C", dxfattribs={"layer": "E-POWR", "height": 0.5})
    t.set_placement((1.0, 1.0))
    r_miss = audit_dxf_bytes(
        _dxf_bytes(doc_miss, encoding="latin-1"),
        required_layers=["E-POWR", "M-DUCT-SUPP"],
        required_text=["TEMP 25", "MISSING LABEL"],
    )
    assert not r_miss["ok"]
    items = {d["item"] for d in r_miss["defects"]}
    assert "dxf_missing_layers" in items
    assert "dxf_missing_text" in items
    assert r_miss["missing_layers"] == ["M-DUCT-SUPP"]
    assert r_miss["missing_text"] == ["MISSING LABEL"]

    # 4. NaN coordinates (mixed with valid entity so bbox alone wouldn't hide it)
    doc_nan = ezdxf.new("R2010")
    doc_nan.modelspace().add_line((0.0, 0.0), (10.0, 5.0))
    doc_nan.modelspace().add_line((math.nan, 0.0), (5.0, 5.0))
    r_nan = audit_dxf_bytes(_dxf_bytes(doc_nan))
    assert not r_nan["ok"]
    assert any(d["item"] == "dxf_non_finite_coordinates" for d in r_nan["defects"])

    # 5. Zero-span degenerate entities
    doc_deg = ezdxf.new("R2010")
    doc_deg.modelspace().add_line((5.0, 5.0), (5.0, 5.0))
    doc_deg.modelspace().add_circle((1.0, 1.0), radius=0.0)
    r_deg = audit_dxf_bytes(_dxf_bytes(doc_deg))
    assert not r_deg["ok"]
    assert any(d["item"] == "dxf_degenerate_entities" for d in r_deg["defects"])


def test_clean_pdf_audit_passes() -> None:
    pdf_bytes = _make_text_pdf(
        [
            ["Section 1: Chilled Water Plant Basis of Design", "Design cooling load: 450 TR (1,582.6 kW)."],
            ["Section 2: Equipment Schedule", "All methodologies follow ASHRAE 90.1 without unresolved holds."],
        ]
    )
    res = audit_pdf_bytes(
        pdf_bytes,
        required_terms=["450 TR", "ASHRAE 90.1"],
        min_pages=2,
    )
    assert res["ok"], res
    assert res["page_count"] == 2
    assert res["non_empty_pages"] == 2
    assert res["placeholders"] == []
    assert res["defects"] == []


def test_defective_pdf_placeholders_min_pages_terms_and_empty_vs_image_only() -> None:
    # 1. Corrupt & empty bytes
    assert not audit_pdf_bytes(b"")["ok"]
    assert not audit_pdf_bytes(b"not-a-pdf")["ok"]

    # 2. Placeholder leaks + min_pages + missing required_terms
    leaky_pdf = _make_text_pdf(
        [
            [
                "Mechanical Specification Draft",
                "Chiller capacity: TODO verify tonnage; Pump head: TBD.",
                "Valve tag: FIXME [INSERT VALVE SCHEDULE]",
                "Cost rollup error: #REF! #VALUE! #DIV/0! #NAME?",
            ]
        ]
    )
    r_leak = audit_pdf_bytes(leaky_pdf, required_terms=["450 TR", "ASHRAE 90.1"], min_pages=2)
    assert not r_leak["ok"]
    assert set(r_leak["placeholders"]) == {
        "TODO",
        "TBD",
        "FIXME",
        "[INSERT",
        "#REF!",
        "#VALUE!",
        "#DIV/0!",
        "#NAME?",
    }
    defect_items = {d["item"] for d in r_leak["defects"]}
    assert {"pdf_min_pages", "pdf_placeholders", "pdf_missing_terms"} <= defect_items

    # 3. Blank zero-text PDF -> hard defect
    r_blank = audit_pdf_bytes(_make_blank_pdf(1))
    assert not r_blank["ok"]
    assert r_blank["non_empty_pages"] == 0
    assert any(d["item"] == "pdf_empty_text" for d in r_blank["defects"])

    # 4. Image-only PDF -> warning (not hard empty-text failure) when no required_terms fail
    r_img = audit_pdf_bytes(_make_image_only_pdf(), min_pages=1)
    assert r_img["ok"], r_img
    assert r_img["non_empty_pages"] == 0
    assert r_img["image_count"] == 1
    assert any(w["item"] == "pdf_image_only" and w["severity"] == "warning" for w in r_img["warnings"])


class _MemorySession:
    def __init__(self, workdir: str = "/workspace") -> None:
        self.workdir = workdir
        self.instruction = "Synthetic CAD/PDF wiring test"
        self.limits: dict[str, int] = {"agent_timeout_sec": 600}
        self.mcp_servers: list[dict] = []
        self.final_message = ""
        self.files: dict[str, bytes] = {}

    def time_left(self) -> float:
        return 600.0

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes | str, append: bool = False) -> int:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.files[path] = raw
        return len(raw)

    async def log(self, text: str) -> None:
        pass

    async def usage(self, *args) -> None:
        pass


def test_harness_dxf_and_pdf_audit_wiring() -> None:
    async def scenario() -> None:
        session = _MemorySession()
        coord = EngineeringCoordinator(session, config=RuntimeConfig.from_env())

        doc = ezdxf.new("R2010")
        doc.layers.add("M-DUCT")
        doc.modelspace().add_line((0.0, 0.0), (15.0, 0.0), dxfattribs={"layer": "M-DUCT"})
        await session.write_file("/workspace/plan.dxf", _dxf_bytes(doc))

        good_pdf = _make_text_pdf([["Basis of Design", "Chiller load 300 TR verified."]])
        bad_pdf = _make_text_pdf([["Draft Report", "Pump head TODO verify."]])
        await session.write_file("/workspace/report.pdf", good_pdf)
        await session.write_file("/workspace/bad.pdf", bad_pdf)

        dxf_res = await coord._run_audit_internal("/workspace/plan.dxf", required_layers=["M-DUCT"])
        assert dxf_res["ok"], dxf_res

        pdf_ok = await coord._run_audit_internal("/workspace/report.pdf", required_terms=["300 TR"])
        assert pdf_ok["ok"], pdf_ok

        pdf_bad = await coord._run_audit_internal("/workspace/bad.pdf")
        assert not pdf_bad["ok"]
        assert "TODO" in pdf_bad["placeholders"]

    asyncio.run(scenario())
