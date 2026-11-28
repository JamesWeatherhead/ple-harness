"""Synthetic 2D viewport calibration and STEP IFC4 clash/self-clash/clearance/translation invariance tests."""

from __future__ import annotations

import math

from verifiers.geometry_ifc_audit import audit_ifc4_bytes, calibrate_viewport_2d, translate_ifc4_step


def _build_synthetic_ifc4(
    *,
    chw_seg2_xmin: float = 4.0,
    ct_zmin: float = 2.4,
) -> bytes:
    """Generates a self-contained valid STEP IFC4 file with nested IfcLocalPlacement + IfcExtrudedAreaSolid."""
    lines = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');",
        "FILE_NAME('synthetic.ifc','2026-11-25T00:00:00',('eng'),('ple'),'ple','ple','');",
        "FILE_SCHEMA(('IFC4'));",
        "ENDSEC;",
        "DATA;",
        # Root coordinate origin (#10/#11/#12) translated by translate_ifc4_step
        "#1=IFCCARTESIANPOINT((0.0,0.0,0.0));",
        "#2=IFCDIRECTION((0.0,0.0,1.0));",
        "#3=IFCDIRECTION((1.0,0.0,0.0));",
        "#4=IFCAXIS2PLACEMENT3D(#1,#2,#3);",
        "#5=IFCLOCALPLACEMENT($,#4);",
        # Direction along +X and +Y for extrusions along Z inside oriented solid frames
        "#6=IFCDIRECTION((0.0,1.0,0.0));",
        "#7=IFCAXIS2PLACEMENT2D(#1,$);",
        # Profile: 0.2m x 0.2m square cross section
        "#20=IFCRECTANGLEPROFILEDEF(.AREA.,'200x200',#7,0.2,0.2);",
        # CHW seg1: extruded 4.0m along +X (axis=Z_dir=(1,0,0), ref=(0,1,0)), centered y=1.0, z=2.0 -> [0..4] x [0.9..1.1] x [1.9..2.1]
        "#30=IFCCARTESIANPOINT((0.0,1.0,2.0));",
        "#31=IFCAXIS2PLACEMENT3D(#30,#3,#6);",
        "#32=IFCLOCALPLACEMENT(#5,#31);",
        "#33=IFCEXTRUDEDAREASOLID(#20,$,#2,4.0);",
        "#34=IFCSHAPEREPRESENTATION($,'Body','SweptSolid',(#33));",
        "#35=IFCPRODUCTDEFINITIONSHAPE($,$,(#34));",
        "#36=IFCPIPESEGMENT('g1',$,'chilled_water:CHW-01',$,$,#32,#35,$);",
        # CHW seg2: extruded from chw_seg2_xmin to 8.0 along +X
        f"#40=IFCCARTESIANPOINT(({chw_seg2_xmin:.6f},1.0,2.0));",
        "#41=IFCAXIS2PLACEMENT3D(#40,#3,#6);",
        "#42=IFCLOCALPLACEMENT(#5,#41);",
        f"#43=IFCEXTRUDEDAREASOLID(#20,$,#2,{8.0 - chw_seg2_xmin:.6f});",
        "#44=IFCSHAPEREPRESENTATION($,'Body','SweptSolid',(#43));",
        "#45=IFCPRODUCTDEFINITIONSHAPE($,$,(#44));",
        "#46=IFCPIPESEGMENT('g2',$,'chilled_water:CHW-02',$,$,#42,#45,$);",
        # Cable tray seg1: runs along +Y (axis=(0,1,0), ref=(0,0,1)) at x=4.0, z=ct_zmin+0.1 from y=0..5 (depth=5.0)
        f"#50=IFCCARTESIANPOINT((4.0,0.0,{ct_zmin + 0.1:.6f}));",
        "#51=IFCAXIS2PLACEMENT3D(#50,#6,#2);",
        "#52=IFCLOCALPLACEMENT(#5,#51);",
        "#53=IFCEXTRUDEDAREASOLID(#20,$,#2,5.0);",
        "#54=IFCSHAPEREPRESENTATION($,'Body','SweptSolid',(#53));",
        "#55=IFCPRODUCTDEFINITIONSHAPE($,$,(#54));",
        "#56=IFCCABLECARRIERSEGMENT('g3',$,'cable_tray:CT-01',$,$,#52,#55,$);",
        "ENDSEC;",
        "END-ISO-10303-21;",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_viewport_2d_calibration_and_heldout_residual_gate() -> None:
    scale = 1.5
    theta = math.radians(37.0)
    tx, ty = 42.0, -18.5

    def fwd(x: float, y: float) -> tuple[float, float]:
        return (
            scale * (math.cos(theta) * x - math.sin(theta) * y) + tx,
            scale * (math.sin(theta) * x + math.cos(theta) * y) + ty,
        )

    anchors = [((x, y), fwd(x, y)) for x, y in [(0.0, 0.0), (10.0, 0.0), (10.0, 8.0), (0.0, 8.0)]]
    good_held = [("H1", (5.0, 4.0), fwd(5.0, 4.0))]
    cal_ok = calibrate_viewport_2d(anchors, heldout_anchors=good_held, max_residual_tol=0.01)
    assert cal_ok.ok
    assert abs(cal_ok.scale_x - scale) < 1e-6
    assert abs(cal_ok.rotation_rad - theta) < 1e-6
    assert cal_ok.max_heldout_residual < 1e-6

    # Perturb held-out anchor beyond tolerance -> must fail gate
    bad_hx, bad_hy = fwd(5.0, 4.0)
    bad_held = [("H_bad", (5.0, 4.0), (bad_hx + 12.0, bad_hy - 5.0))]
    cal_bad = calibrate_viewport_2d(anchors, heldout_anchors=bad_held, max_residual_tol=2.0)
    assert not cal_bad.ok
    assert cal_bad.max_heldout_residual > 12.0


def test_clean_ifc4_and_translation_invariance() -> None:
    clean_ifc = _build_synthetic_ifc4()
    src = {"chilled_water": (0.0, 1.0, 2.0), "cable_tray": (4.0, 0.0, 2.5)}
    req = {"chilled_water": [(8.0, 1.0, 2.0)], "cable_tray": [(4.0, 5.0, 2.5)]}

    res = audit_ifc4_bytes(clean_ifc, required_endpoints=req, source_points=src, min_clearance=0.05)
    assert res.ok, res.defects
    assert res.cross_service_clashes == []
    assert res.same_service_clashes == []
    assert res.unserved_endpoints == []
    assert abs(res.total_route_length - 13.0) < 1e-4  # 4 + 4 + 5
    assert res.min_cross_clearance is not None and res.min_cross_clearance >= 0.29

    # Rigidly translate root IfcLocalPlacement + sources + endpoints -> route length & feasibility invariant
    dx, dy, dz = 125.0, -47.5, 18.25
    shifted_ifc = translate_ifc4_step(clean_ifc, dx, dy, dz)
    shifted_src = {k: (v[0] + dx, v[1] + dy, v[2] + dz) for k, v in src.items()}
    shifted_req = {k: [(p[0] + dx, p[1] + dy, p[2] + dz) for p in pts] for k, pts in req.items()}

    res_shifted = audit_ifc4_bytes(shifted_ifc, required_endpoints=shifted_req, source_points=shifted_src, min_clearance=0.05)
    assert res_shifted.ok, res_shifted.defects
    assert abs(res_shifted.total_route_length - res.total_route_length) < 1e-4
    assert abs((res_shifted.min_cross_clearance or 0.0) - (res.min_cross_clearance or 0.0)) < 1e-4


def test_ifc4_injected_clashes_self_clash_unserved_and_forbidden_zone() -> None:
    src = {"chilled_water": (0.0, 1.0, 2.0), "cable_tray": (4.0, 0.0, 2.5)}
    req = {"chilled_water": [(8.0, 1.0, 2.0)], "cable_tray": [(4.0, 5.0, 2.5)]}

    # Cross-service clash (cable tray lowered into chilled water at z=1.95..2.15 crosses x=4.0 seam -> clashes with CHW-01 & CHW-02)
    res_cross = audit_ifc4_bytes(_build_synthetic_ifc4(ct_zmin=1.95))
    assert not res_cross.ok
    assert len(res_cross.cross_service_clashes) >= 1
    assert any(c["b"] == "cable_tray:CT-01" for c in res_cross.cross_service_clashes)

    # 2. Same-service corner self-clash (chw_seg2 starts at x=3.85 overlapping seg1 x=0..4.0 by 0.15m)
    res_self = audit_ifc4_bytes(_build_synthetic_ifc4(chw_seg2_xmin=3.85))
    assert not res_self.ok
    assert len(res_self.same_service_clashes) == 1
    assert res_self.same_service_clashes[0]["penetration"] > 0.14

    # 3. Unserved endpoint via BFS connectivity
    res_unserved = audit_ifc4_bytes(
        _build_synthetic_ifc4(),
        source_points=src,
        required_endpoints={"chilled_water": [(25.0, 1.0, 2.0)]},
    )
    assert not res_unserved.ok
    assert len(res_unserved.unserved_endpoints) == 1

    # 4. Forbidden aisle / corridor violation
    res_zone = audit_ifc4_bytes(
        _build_synthetic_ifc4(),
        forbidden_zones=[
            {"name": "MainAisle", "xmin": 3.5, "xmax": 4.5, "ymin": 3.0, "ymax": 4.5, "zmin": 0.0, "zmax": 3.0, "services": ["cable_tray"]}
        ],
    )
    assert not res_zone.ok
    assert len(res_zone.forbidden_zone_violations) == 1
    assert res_zone.forbidden_zone_violations[0]["zone"] == "MainAisle"


def test_ifc4_representation_contexts_and_spatial_structure_gates() -> None:
    """Tests that IfcProject exports missing RepresentationContexts or spatial containers fail deterministically."""
    base_lines = _build_synthetic_ifc4().decode("utf-8").splitlines()
    end_idx = len(base_lines) - 1 - base_lines[::-1].index("ENDSEC;")

    bad_lines = (
        base_lines[:end_idx]
        + ["#90=IFCPROJECT('p1',$,'MEP',$,$,$,$,$,$);"]
        + base_lines[end_idx:]
    )
    res_bad = audit_ifc4_bytes("\n".join(bad_lines).encode("utf-8"))
    assert not res_bad.ok
    defect_items = {d["item"] for d in res_bad.defects}
    assert "missing_representation_contexts" in defect_items
    assert "missing_spatial_structure" in defect_items

    good_lines = (
        base_lines[:end_idx]
        + [
            "#91=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.0E-5,#4,$);",
            "#90=IFCPROJECT('p1',$,'MEP',$,$,$,$,(#91),$);",
            "#92=IFCBUILDING('b1',$,'DataHall',$,$,#5,$,$,.ELEMENT.,$,$,$);",
        ]
        + base_lines[end_idx:]
    )
    res_good = audit_ifc4_bytes("\n".join(good_lines).encode("utf-8"))
    assert res_good.ok, res_good.defects
