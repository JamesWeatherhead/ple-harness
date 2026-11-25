# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Independent decision-memo and simulated application-state readback auditor."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from verifiers.workbook_audit import make_defect


def extract_docx_text(docx_bytes: bytes) -> str:
    """Extracts plain text from a `.docx` file's `word/document.xml` without extra C deps."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        xml_text = zf.read("word/document.xml").decode("utf-8", errors="replace")
    # Preserve paragraph breaks while stripping tags
    xml_text = re.sub(r"</w:p\s*>", "\n", xml_text)
    xml_text = re.sub(r"<[^>]+>", "", xml_text)
    return re.sub(r"\n{3,}", "\n\n", xml_text).strip()


def evaluate_energization_decision(
    requested_date: str,
    prerequisites: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministically derives an energization/PTO decision from structured prerequisites.

    Each prerequisite dict contains:
      - `id`: identifier (e.g., "WITNESS_TEST", "RELAY_SETTINGS")
      - `status`: "completed" | "approved" | "pending" | "failed" | "proposed"
      - `required_before_energization`: bool (default True)
      - `effective_date`: optional ISO YYYY-MM-DD
    """
    blocking: list[dict[str, Any]] = []
    proposed_only: list[dict[str, Any]] = []
    satisfied: list[dict[str, Any]] = []

    for item in prerequisites:
        req = bool(item.get("required_before_energization", True))
        status = str(item.get("status", "")).strip().lower()
        eff_date = str(item.get("effective_date") or "").strip()
        if status in ("completed", "approved") and (not eff_date or not requested_date or eff_date <= requested_date):
            satisfied.append(item)
        elif req:
            if status == "proposed":
                proposed_only.append(item)
            blocking.append(item)

    approved_for_date = len(blocking) == 0
    return {
        "requested_date": requested_date,
        "decision": "APPROVED" if approved_for_date else "NOT_APPROVED_PENDING",
        "approved_for_requested_date": approved_for_date,
        "blocking_items": [b.get("id") for b in blocking],
        "proposed_not_completed": [p.get("id") for p in proposed_only],
        "satisfied_items": [s.get("id") for s in satisfied],
    }


def audit_decision_memo(
    memo_bytes: bytes,
    *,
    is_docx: bool = False,
    requested_date: str = "",
    prerequisites: list[dict[str, Any]] | None = None,
    expected_work_package: dict[str, Any] | None = None,
    actual_work_package: dict[str, Any] | None = None,
    required_sections: tuple[str, ...] = ("decision", "scope", "prerequisite", "change"),
) -> dict[str, Any]:
    """Audits a technical decision memo (.md/.txt/.docx) and reconciles MCP work-package state."""
    defects: list[dict[str, Any]] = []
    text = extract_docx_text(memo_bytes) if is_docx else memo_bytes.decode("utf-8", errors="replace")
    clean = text.strip()

    if len(clean) < 20:
        defects.append(
            make_defect(
                item="memo_non_empty",
                requirement_source="Deliverable completeness check",
                measured=len(clean),
                limit=">= 20 chars",
                tolerance=0,
                earliest_suspect_dependency="Deliverable file generation",
            )
        )
        return {"ok": False, "text_length": len(clean), "defects": defects}

    low = clean.lower()
    if requested_date and requested_date.lower() not in low:
        defects.append(
            make_defect(
                item="requested_date",
                requirement_source="Decision memo date check",
                measured="missing",
                limit=requested_date,
                tolerance=0,
                earliest_suspect_dependency="Memo header / request summary",
            )
        )

    for sec in required_sections:
        if sec.lower() not in low:
            defects.append(
                make_defect(
                    item=f"section:{sec}",
                    requirement_source="Decision checklist coverage",
                    measured="missing",
                    limit=f"contains '{sec}'",
                    tolerance=0,
                    earliest_suspect_dependency=f"Memo section addressing {sec}",
                )
            )

    derived = None
    if prerequisites is not None:
        derived = evaluate_energization_decision(requested_date, prerequisites)
        # Verify memo reflects every blocking item when NOT_APPROVED_PENDING, and doesn't claim premature approval
        if not derived["approved_for_requested_date"]:
            claims_approved = bool(
                re.search(r"\b(is\s+approved|grant\s+permission|can\s+be\s+energized\s+on)\b", low)
                and not re.search(r"\b(cannot|not\s+approved|denied|withheld|pending|blocked)\b", low)
            )
            if claims_approved:
                defects.append(
                    make_defect(
                        item="decision_conclusion",
                        requirement_source="Prerequisite status gate",
                        measured="claims approved despite blockers",
                        limit=f"NOT_APPROVED_PENDING ({derived['blocking_items']})",
                        tolerance=0,
                        earliest_suspect_dependency=f"Prerequisite gate: {derived['blocking_items']}",
                    )
                )
            for bid in derived["blocking_items"]:
                if str(bid).lower() not in low:
                    defects.append(
                        make_defect(
                            item=f"blocker_citation:{bid}",
                            requirement_source="Outstanding prerequisite citation",
                            measured="omitted",
                            limit=str(bid),
                            tolerance=0,
                            earliest_suspect_dependency=f"Prerequisite record for {bid}",
                        )
                    )

    if expected_work_package is not None:
        if not actual_work_package:
            defects.append(
                make_defect(
                    item="mcp_work_package",
                    requirement_source="Application action read-back",
                    measured="not found",
                    limit=str(expected_work_package),
                    tolerance=0,
                    earliest_suspect_dependency="Create/update work package via MCP tool",
                )
            )
        else:
            for k, exp_v in expected_work_package.items():
                got_v = actual_work_package.get(k)
                if str(got_v).strip() != str(exp_v).strip():
                    defects.append(
                        make_defect(
                            item=f"work_package.{k}",
                            requirement_source="MCP work-package reconciliation",
                            measured=got_v,
                            limit=exp_v,
                            tolerance="exact match",
                            earliest_suspect_dependency=f"MCP work package field {k}",
                        )
                    )

    return {
        "ok": len(defects) == 0,
        "text_length": len(clean),
        "derived_decision": derived,
        "defects": defects,
    }
