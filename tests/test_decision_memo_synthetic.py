"""Synthetic permission-to-operate / energization decision memo & MCP work-package audit tests."""

from __future__ import annotations

import io
from docx import Document

from verifiers.decision_memo_audit import audit_decision_memo, evaluate_energization_decision


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_clean_decision_memo_markdown_and_docx_pass() -> None:
    prereqs = [
        {"id": "PR-01", "status": "approved", "completed_date": "2026-10-01"},
        {"id": "PR-02", "status": "completed", "completed_date": "2026-10-05"},
        {"id": "PR-03", "status": "approved", "completed_date": "2026-10-10"},
    ]
    eval_res = evaluate_energization_decision("2026-10-15", prereqs)
    assert eval_res["decision"] == "APPROVED"
    assert eval_res["approved_for_requested_date"] is True
    assert eval_res["blocking_items"] == []
    assert eval_res["satisfied_items"] == ["PR-01", "PR-02", "PR-03"]

    pkg = {"work_package_id": "WP-138", "status": "APPROVED_FOR_PTO", "target_date": "2026-10-15"}
    memo_lines = [
        "Decision: Feeder 13.8 kV circuit is approved for requested date 2026-10-15.",
        "Scope: Substation SS-1 Feeder F1 to MB-1 under Work Package WP-138.",
        "Prerequisite Status: PR-01 approved (2026-10-01), PR-02 completed (2026-10-05), PR-03 approved (2026-10-10).",
        "Change Assessment: No outstanding holds remain before energization.",
    ]
    md_audit = audit_decision_memo(
        "\n".join(memo_lines).encode("utf-8"),
        is_docx=False,
        requested_date="2026-10-15",
        prerequisites=prereqs,
        expected_work_package=pkg,
        actual_work_package=dict(pkg),
    )
    assert md_audit["ok"], md_audit

    docx_audit = audit_decision_memo(
        _make_docx_bytes(memo_lines),
        is_docx=True,
        requested_date="2026-10-15",
        prerequisites=prereqs,
        expected_work_package=pkg,
        actual_work_package=dict(pkg),
    )
    assert docx_audit["ok"], docx_audit


def test_single_prerequisite_flip_inverts_decision_without_perturbing_others() -> None:
    prereqs = [
        {"id": "PR-01", "status": "approved", "completed_date": "2026-10-01"},
        {"id": "PR-02", "status": "completed", "completed_date": "2026-10-05"},
        {"id": "PR-03", "status": "pending", "completed_date": ""},  # flipped 1 status
        {"id": "PR-04", "status": "completed", "completed_date": "2026-10-09"},
    ]
    eval_res = evaluate_energization_decision("2026-10-15", prereqs)
    assert eval_res["decision"] == "NOT_APPROVED_PENDING"
    assert eval_res["approved_for_requested_date"] is False
    assert eval_res["blocking_items"] == ["PR-03"]
    # Unrelated satisfied items remain strictly invariant
    assert eval_res["satisfied_items"] == ["PR-01", "PR-02", "PR-04"]

    # Stale approval memo written before PR-03 flipped to pending must fail loudly
    stale_memo = _make_docx_bytes([
        "Decision: PTO is approved on 2026-10-15.",
        "Scope: Substation SS-1 Feeder F1.",
        "Prerequisite Review: PR-01, PR-02, PR-04 complete.",
        "Change Control: Updated work package submitted.",
    ])
    bad_audit = audit_decision_memo(
        stale_memo,
        is_docx=True,
        requested_date="2026-10-15",
        prerequisites=prereqs,
    )
    assert not bad_audit["ok"]
    items = {d["item"] for d in bad_audit["defects"]}
    assert "decision_conclusion" in items
    assert "blocker_citation:PR-03" in items

    # Repaired memo citing NOT_APPROVED_PENDING + PR-03 passes
    fixed_memo = _make_docx_bytes([
        "Decision: Permission to operate is NOT approved (NOT_APPROVED_PENDING) for requested date 2026-10-15.",
        "Scope: Substation SS-1 Feeder F1 under Work Package WP-138.",
        "Prerequisite Status: PR-01, PR-02, PR-04 satisfied; PR-03 remains pending and blocks energization.",
        "Change Control: Updated MCP package state to HOLD_PENDING_PR03.",
    ])
    expected_wp = {"work_package_id": "WP-138", "status": "HOLD_PENDING_PR03"}
    fixed_audit = audit_decision_memo(
        fixed_memo,
        is_docx=True,
        requested_date="2026-10-15",
        prerequisites=prereqs,
        expected_work_package=expected_wp,
        actual_work_package={"work_package_id": "WP-138", "status": "HOLD_PENDING_PR03"},
    )
    assert fixed_audit["ok"], fixed_audit

    # MCP work-package state mismatch must fail cleanly
    mismatch_audit = audit_decision_memo(
        fixed_memo,
        is_docx=True,
        requested_date="2026-10-15",
        prerequisites=prereqs,
        expected_work_package=expected_wp,
        actual_work_package={"work_package_id": "WP-138", "status": "APPROVED_FOR_PTO"},
    )
    assert not mismatch_audit["ok"]
    assert any(d["item"] == "work_package.status" for d in mismatch_audit["defects"])
