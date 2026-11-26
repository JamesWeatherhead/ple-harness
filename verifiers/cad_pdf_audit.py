# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Independent cold-read CAD (.dxf) and drawing/specification (.pdf) deliverable auditors."""

from __future__ import annotations

import io
import math
import re
from typing import Any

import ezdxf
from ezdxf import bbox as ezdxf_bbox
import pypdf

from verifiers.workbook_audit import make_defect

_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("TODO", re.compile(r"\bTODO\b")),
    ("TBD", re.compile(r"\bTBD\b", re.I)),
    ("FIXME", re.compile(r"\bFIXME\b", re.I)),
    ("[INSERT", re.compile(r"\[INSERT\b", re.I)),
    ("#REF!", re.compile(r"#REF!")),
    ("#VALUE!", re.compile(r"#VALUE!")),
    ("#DIV/0!", re.compile(r"#DIV/0!")),
    ("#NAME?", re.compile(r"#NAME\?")),
)


def _decode_dxf_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _all_numbers_finite(val: Any) -> bool:
    if isinstance(val, bool):
        return True
    if isinstance(val, (int, float)):
        return math.isfinite(float(val))
    # Handles Vec2, Vec3, tuples, lists
    if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
        return all(_all_numbers_finite(item) for item in val)
    return True


def _extract_entity_text(entity: Any) -> list[str]:
    out: list[str] = []
    dxftype = entity.dxftype()
    if dxftype == "MTEXT":
        try:
            txt = entity.plain_text()
        except Exception:
            txt = str(getattr(entity, "text", "") or getattr(entity.dxf, "text", "") or "")
        if txt.strip():
            out.append(txt.strip())
    elif dxftype in ("TEXT", "ATTRIB", "ATTDEF"):
        txt = str(getattr(entity.dxf, "text", "") or "").strip()
        if txt:
            out.append(txt)
    elif dxftype == "DIMENSION":
        txt = str(getattr(entity.dxf, "text", "") or "").strip()
        if txt and txt != "<>":
            out.append(txt)
    elif dxftype == "INSERT":
        for att in getattr(entity, "attribs", []) or []:
            txt = str(getattr(att.dxf, "text", "") or "").strip()
            if txt:
                out.append(txt)
    return out


def _inspect_entity_geometry(entity: Any, idx: int) -> tuple[bool, str | None]:
    """Returns `(has_non_finite, degenerate_description_or_none)` for one DXF entity."""
    dxftype = entity.dxftype()
    label = f"{dxftype}#{idx}"

    try:
        attribs = entity.dxf.all_existing_dxf_attribs()
    except Exception:
        attribs = {}

    for attr_val in attribs.values():
        if not _all_numbers_finite(attr_val):
            return True, None

    if dxftype == "LWPOLYLINE":
        pts = [tuple(p) for p in entity.get_points("xy")]
        if not all(_all_numbers_finite(p) for p in pts):
            return True, None
        if len(pts) < 2:
            return False, f"{label} (<2 vertices)"
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        if max(xs) - min(xs) <= 1e-9 and max(ys) - min(ys) <= 1e-9:
            return False, f"{label} (zero-span polyline)"
    elif dxftype == "POLYLINE":
        pts = [tuple(p) for p in entity.points()]
        if not all(_all_numbers_finite(p) for p in pts):
            return True, None
        if len(pts) < 2:
            return False, f"{label} (<2 vertices)"
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        zs = [float(p[2]) if len(p) > 2 else 0.0 for p in pts]
        if max(xs) - min(xs) <= 1e-9 and max(ys) - min(ys) <= 1e-9 and max(zs) - min(zs) <= 1e-9:
            return False, f"{label} (zero-span polyline)"
    elif dxftype == "SPLINE":
        for pts_attr in ("control_points", "fit_points"):
            pts = list(getattr(entity, pts_attr, []) or [])
            if any(not _all_numbers_finite(tuple(p)) for p in pts):
                return True, None
    elif dxftype == "LINE":
        s = entity.dxf.start
        e = entity.dxf.end
        if math.dist((float(s.x), float(s.y), float(s.z)), (float(e.x), float(e.y), float(e.z))) <= 1e-9:
            return False, f"{label} (zero-length line)"
    elif dxftype in ("CIRCLE", "ARC"):
        radius = float(getattr(entity.dxf, "radius", 0.0))
        if radius <= 1e-9:
            return False, f"{label} (radius={radius})"
    elif dxftype == "ELLIPSE":
        maj = entity.dxf.major_axis
        ratio = float(getattr(entity.dxf, "ratio", 1.0))
        if math.hypot(float(maj.x), float(maj.y), float(maj.z)) <= 1e-9 or ratio <= 1e-9:
            return False, f"{label} (zero-span ellipse)"
    elif dxftype == "INSERT":
        xs = abs(float(getattr(entity.dxf, "xscale", 1.0)))
        ys = abs(float(getattr(entity.dxf, "yscale", 1.0)))
        if xs <= 1e-9 or ys <= 1e-9:
            return False, f"{label} (zero-scale insert)"

    return False, None


def audit_dxf_bytes(
    data: bytes,
    required_layers: list[str] | None = None,
    required_text: list[str] | None = None,
) -> dict[str, Any]:
    """Cold-reads DXF bytes via ezdxf and checks entity count, finiteness, bounds, layers, and text."""
    defects: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not data or not data.strip():
        defects.append(
            make_defect(
                item="dxf_parse",
                requirement_source="DXF cold-read parse",
                measured="empty bytes",
                limit="valid DXF document",
                tolerance=0,
                earliest_suspect_dependency="DXF export file generation",
            )
        )
        return {
            "ok": False,
            "entity_count": 0,
            "layers": [],
            "bbox": None,
            "text_snippets": [],
            "missing_layers": list(required_layers or []),
            "missing_text": list(required_text or []),
            "degenerate_entities": [],
            "non_finite_entities": [],
            "defects": defects,
            "warnings": warnings,
        }

    try:
        text = _decode_dxf_text(data)
        doc = ezdxf.read(io.StringIO(text))
    except Exception as exc:
        defects.append(
            make_defect(
                item="dxf_parse",
                requirement_source="DXF cold-read parse",
                measured=f"{type(exc).__name__}: {exc}",
                limit="valid DXF document",
                tolerance=0,
                earliest_suspect_dependency="DXF export formatting / header structure",
            )
        )
        return {
            "ok": False,
            "entity_count": 0,
            "layers": [],
            "bbox": None,
            "text_snippets": [],
            "missing_layers": list(required_layers or []),
            "missing_text": list(required_text or []),
            "degenerate_entities": [],
            "non_finite_entities": [],
            "defects": defects,
            "warnings": warnings,
        }

    msp = doc.modelspace()
    entities = list(msp)
    entity_count = len(entities)

    doc_layers = {str(layer.dxf.name) for layer in doc.layers if getattr(layer.dxf, "name", "")}
    entity_layers = {str(getattr(e.dxf, "layer", "")) for e in entities if getattr(e.dxf, "layer", "")}
    layers = sorted(doc_layers | entity_layers)

    if entity_count == 0:
        defects.append(
            make_defect(
                item="dxf_modelspace_empty",
                requirement_source="DXF modelspace completeness check",
                measured=0,
                limit=">= 1 entity",
                tolerance=0,
                earliest_suspect_dependency="DXF modelspace entity generation",
            )
        )

    non_finite_entities: list[str] = []
    degenerate_entities: list[str] = []
    text_snippets: list[str] = []

    for idx, entity in enumerate(entities):
        bad_num, deg_desc = _inspect_entity_geometry(entity, idx)
        if bad_num:
            non_finite_entities.append(f"{entity.dxftype()}#{idx}")
        if deg_desc is not None:
            degenerate_entities.append(deg_desc)
        text_snippets.extend(_extract_entity_text(entity))

    bbox: list[float] | None = None
    if entity_count > 0:
        try:
            ext = ezdxf_bbox.extents(msp, fast=True)
            if ext.has_data:
                coords = [
                    float(ext.extmin.x),
                    float(ext.extmin.y),
                    float(ext.extmin.z),
                    float(ext.extmax.x),
                    float(ext.extmax.y),
                    float(ext.extmax.z),
                ]
                if all(math.isfinite(c) for c in coords):
                    bbox = [round(c, 6) for c in coords]
                else:
                    non_finite_entities.append("modelspace_bbox")
        except Exception as exc:
            warnings.append(
                make_defect(
                    item="dxf_bbox_eval",
                    requirement_source="DXF bounding box calculation",
                    measured=f"{type(exc).__name__}: {exc}",
                    limit="computable extents",
                    tolerance=0,
                    earliest_suspect_dependency="DXF entity geometry",
                    severity="warning",
                )
            )

    if non_finite_entities:
        defects.append(
            make_defect(
                item="dxf_non_finite_coordinates",
                requirement_source="DXF finite coordinate integrity",
                measured=non_finite_entities,
                limit="all finite coordinates",
                tolerance=0,
                earliest_suspect_dependency=f"Geometry computation producing NaN/Inf on {non_finite_entities[0]}",
            )
        )

    if degenerate_entities:
        defects.append(
            make_defect(
                item="dxf_degenerate_entities",
                requirement_source="DXF non-degenerate geometry check",
                measured=degenerate_entities,
                limit="0 zero-span/degenerate entities",
                tolerance=0,
                earliest_suspect_dependency=f"Zero-span entity generation: {degenerate_entities[0]}",
            )
        )
    elif bbox is not None and entity_count > 0:
        dx = bbox[3] - bbox[0]
        dy = bbox[4] - bbox[1]
        dz = bbox[5] - bbox[2]
        is_single_point = entity_count == 1 and entities[0].dxftype() == "POINT"
        if dx <= 1e-9 and dy <= 1e-9 and dz <= 1e-9 and not is_single_point:
            defects.append(
                make_defect(
                    item="dxf_degenerate_bbox",
                    requirement_source="DXF non-degenerate bounding box check",
                    measured=bbox,
                    limit="non-zero spatial extent",
                    tolerance=0,
                    earliest_suspect_dependency="Modelspace coordinate scaling / collapsed geometry",
                )
            )

    missing_layers: list[str] = []
    if required_layers:
        lower_layers = {layer.lower() for layer in layers}
        missing_layers = [req for req in required_layers if req.strip().lower() not in lower_layers]
        if missing_layers:
            defects.append(
                make_defect(
                    item="dxf_missing_layers",
                    requirement_source="DXF layer inventory check",
                    measured=layers,
                    limit=list(required_layers),
                    tolerance="exact layer coverage",
                    earliest_suspect_dependency=f"DXF layer creation for {missing_layers}",
                )
            )

    missing_text: list[str] = []
    if required_text:
        joined = re.sub(r"\s+", " ", "\n".join(text_snippets)).lower()
        missing_text = [
            req for req in required_text if re.sub(r"\s+", " ", req.strip()).lower() not in joined
        ]
        if missing_text:
            defects.append(
                make_defect(
                    item="dxf_missing_text",
                    requirement_source="DXF TEXT/MTEXT annotation check",
                    measured=text_snippets[:10],
                    limit=list(required_text),
                    tolerance="substring match",
                    earliest_suspect_dependency=f"DXF TEXT/MTEXT labels missing: {missing_text}",
                )
            )

    return {
        "ok": len(defects) == 0,
        "entity_count": entity_count,
        "layers": layers,
        "bbox": bbox,
        "text_snippets": text_snippets,
        "missing_layers": missing_layers,
        "missing_text": missing_text,
        "degenerate_entities": degenerate_entities,
        "non_finite_entities": non_finite_entities,
        "defects": defects,
        "warnings": warnings,
    }


def _count_page_images(page: Any) -> int:
    try:
        resources = page.get("/Resources")
        if resources is not None:
            resources = resources.get_object()
            xobjs = resources.get("/XObject")
            if xobjs is not None:
                xobjs = xobjs.get_object()
                count = 0
                for key in xobjs:
                    obj = xobjs[key].get_object()
                    if str(obj.get("/Subtype", "")) == "/Image":
                        count += 1
                return count
    except Exception:
        pass
    try:
        return len(page.images)
    except Exception:
        return 0


def audit_pdf_bytes(
    data: bytes,
    required_terms: list[str] | None = None,
    min_pages: int = 1,
) -> dict[str, Any]:
    """Cold-reads PDF bytes via pypdf and checks page count, readable text, placeholders, and required terms."""
    defects: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not data or not data.strip():
        defects.append(
            make_defect(
                item="pdf_parse",
                requirement_source="PDF cold-read parse",
                measured="empty bytes",
                limit="valid PDF document",
                tolerance=0,
                earliest_suspect_dependency="PDF export file generation",
            )
        )
        return {
            "ok": False,
            "page_count": 0,
            "non_empty_pages": 0,
            "image_count": 0,
            "placeholders": [],
            "missing_terms": list(required_terms or []),
            "text_length": 0,
            "defects": defects,
            "warnings": warnings,
        }

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = list(reader.pages)
    except Exception as exc:
        defects.append(
            make_defect(
                item="pdf_parse",
                requirement_source="PDF cold-read parse",
                measured=f"{type(exc).__name__}: {exc}",
                limit="valid PDF document",
                tolerance=0,
                earliest_suspect_dependency="PDF file structure / export stream",
            )
        )
        return {
            "ok": False,
            "page_count": 0,
            "non_empty_pages": 0,
            "image_count": 0,
            "placeholders": [],
            "missing_terms": list(required_terms or []),
            "text_length": 0,
            "defects": defects,
            "warnings": warnings,
        }

    page_count = len(pages)
    if page_count < min_pages:
        defects.append(
            make_defect(
                item="pdf_min_pages",
                requirement_source="PDF page count check",
                measured=page_count,
                limit=f">= {min_pages}",
                tolerance=0,
                earliest_suspect_dependency="PDF page rendering / section completeness",
            )
        )

    page_texts: list[str] = []
    non_empty_pages = 0
    image_count = 0
    for idx, page in enumerate(pages):
        try:
            txt = page.extract_text() or ""
        except Exception as exc:
            txt = ""
            warnings.append(
                make_defect(
                    item=f"pdf_page_{idx + 1}_extract",
                    requirement_source="PDF text extraction",
                    measured=f"{type(exc).__name__}: {exc}",
                    limit="readable page text",
                    tolerance=0,
                    earliest_suspect_dependency=f"PDF page {idx + 1} content stream",
                    severity="warning",
                )
            )
        if txt.strip():
            non_empty_pages += 1
        page_texts.append(txt)
        image_count += _count_page_images(page)

    full_text = "\n".join(page_texts)
    clean_text = full_text.strip()

    if non_empty_pages == 0:
        if image_count > 0:
            warnings.append(
                make_defect(
                    item="pdf_image_only",
                    requirement_source="PDF extractable text check",
                    measured=f"0 text pages, {image_count} embedded image(s)",
                    limit=">= 1 text page or image-only drawing",
                    tolerance=0,
                    earliest_suspect_dependency="Raster-only PDF export (no OCR/vector text layer)",
                    severity="warning",
                )
            )
        else:
            defects.append(
                make_defect(
                    item="pdf_empty_text",
                    requirement_source="PDF readable text check",
                    measured=0,
                    limit=">= 1 non-empty page",
                    tolerance=0,
                    earliest_suspect_dependency="PDF content rendering (blank/unreadable pages)",
                )
            )

    placeholders: list[str] = []
    for token, pattern in _PLACEHOLDER_PATTERNS:
        if pattern.search(full_text):
            placeholders.append(token)

    if placeholders:
        defects.append(
            make_defect(
                item="pdf_placeholders",
                requirement_source="PDF unfinished placeholder check",
                measured=placeholders,
                limit=[],
                tolerance=0,
                earliest_suspect_dependency=f"Draft placeholder tokens leaked into PDF: {placeholders}",
            )
        )

    missing_terms: list[str] = []
    if required_terms:
        norm_text = re.sub(r"\s+", " ", clean_text).lower()
        missing_terms = [
            term for term in required_terms if re.sub(r"\s+", " ", term.strip()).lower() not in norm_text
        ]
        if missing_terms:
            defects.append(
                make_defect(
                    item="pdf_missing_terms",
                    requirement_source="PDF required technical content check",
                    measured=missing_terms,
                    limit=list(required_terms),
                    tolerance="substring match",
                    earliest_suspect_dependency=f"PDF narrative missing required terms: {missing_terms}",
                )
            )

    return {
        "ok": len(defects) == 0,
        "page_count": page_count,
        "non_empty_pages": non_empty_pages,
        "image_count": image_count,
        "placeholders": placeholders,
        "missing_terms": missing_terms,
        "text_length": len(clean_text),
        "defects": defects,
        "warnings": warnings,
    }
