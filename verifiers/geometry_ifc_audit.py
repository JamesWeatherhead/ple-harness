# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Independent 2D viewport calibration and STEP IFC4 3D MEP routing geometry auditor."""

from __future__ import annotations

import dataclasses
import math
import re
from collections import deque
from typing import Any


@dataclasses.dataclass(frozen=True)
class ViewportCalibration:
    scale_x: float
    scale_y: float
    rotation_rad: float
    tx: float
    ty: float
    train_rms: float
    heldout_residuals: list[dict[str, Any]]
    max_heldout_residual: float
    ok: bool


def calibrate_viewport_2d(
    anchors: list[tuple[tuple[float, float], tuple[float, float]]],
    heldout_anchors: list[tuple[str, tuple[float, float], tuple[float, float]]] | None = None,
    max_residual_tol: float = 5.0,
) -> ViewportCalibration:
    """Fits a 2D similarity/rigid transform (with optional scale) from drawing to world coords.

    Fits `(wx, wy) = s * R(theta) * (dx, dy) + (tx, ty)` using least squares over `anchors`,
    then evaluates held-out unused anchors (`heldout_anchors`) against `max_residual_tol`.
    """
    if len(anchors) < 2:
        raise ValueError("Need at least 2 labeled grid intersection anchors for 2D calibration")

    # Solve linear similarity: wx = a*dx - b*dy + tx ; wy = b*dx + a*dy + ty
    # via normal equations on 4 unknowns [a, b, tx, ty]
    n = len(anchors)
    sum_dx = sum(p[0][0] for p in anchors)
    sum_dy = sum(p[0][1] for p in anchors)
    sum_wx = sum(p[1][0] for p in anchors)
    sum_wy = sum(p[1][1] for p in anchors)

    mdx, mdy = sum_dx / n, sum_dy / n
    mwx, mwy = sum_wx / n, sum_wy / n

    num_a = sum((p[0][0] - mdx) * (p[1][0] - mwx) + (p[0][1] - mdy) * (p[1][1] - mwy) for p in anchors)
    num_b = sum((p[0][0] - mdx) * (p[1][1] - mwy) - (p[0][1] - mdy) * (p[1][0] - mwx) for p in anchors)
    den = sum((p[0][0] - mdx) ** 2 + (p[0][1] - mdy) ** 2 for p in anchors)
    if den <= 1e-12:
        raise ValueError("Degenerate anchor points (collocated)")
    a = num_a / den
    b = num_b / den
    tx = mwx - (a * mdx - b * mdy)
    ty = mwy - (b * mdx + a * mdy)

    scale = math.hypot(a, b)
    rot = math.atan2(b, a)

    def xform(dx: float, dy: float) -> tuple[float, float]:
        return (a * dx - b * dy + tx, b * dx + a * dy + ty)

    sq_errs = []
    for (dx, dy), (wx, wy) in anchors:
        px, py = xform(dx, dy)
        sq_errs.append((px - wx) ** 2 + (py - wy) ** 2)
    train_rms = math.sqrt(sum(sq_errs) / len(sq_errs))

    heldout_res: list[dict[str, Any]] = []
    max_held = 0.0
    for label, (dx, dy), (wx, wy) in heldout_anchors or []:
        px, py = xform(dx, dy)
        err = math.hypot(px - wx, py - wy)
        max_held = max(max_held, err)
        heldout_res.append(
            {
                "anchor": label,
                "predicted": (round(px, 4), round(py, 4)),
                "expected": (wx, wy),
                "residual": round(err, 4),
            }
        )

    ok = max_held <= max_residual_tol and train_rms <= max_residual_tol
    return ViewportCalibration(
        scale_x=scale,
        scale_y=scale,
        rotation_rad=rot,
        tx=tx,
        ty=ty,
        train_rms=train_rms,
        heldout_residuals=heldout_res,
        max_heldout_residual=max_held,
        ok=ok,
    )


# ---------------------------------------------------------------------------
# Pure-Python STEP IFC4 Parser & Independent 3D Geometry Auditor
# ---------------------------------------------------------------------------

_ENTITY_RE = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\)\s*;", re.I | re.S)


def _split_step_args(arg_str: str) -> list[str]:
    """Splits top-level comma-separated STEP entity arguments respecting parens and quotes."""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(arg_str):
        ch = arg_str[i]
        if ch == "'":
            if in_str and i + 1 < len(arg_str) and arg_str[i + 1] == "'":
                cur.append("''")
                i += 2
                continue
            in_str = not in_str
            cur.append(ch)
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append("".join(cur).strip())
                cur = []
                i += 1
                continue
            cur.append(ch)
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _parse_ref(tok: str) -> int | None:
    tok = tok.strip()
    if tok.startswith("#") and tok[1:].isdigit():
        return int(tok[1:])
    return None


def _parse_float_tuple(tok: str) -> tuple[float, ...]:
    tok = tok.strip()
    if tok.startswith("(") and tok.endswith(")"):
        tok = tok[1:-1]
    return tuple(float(x.strip()) for x in tok.split(",") if x.strip())


def _parse_ref_list(tok: str) -> list[int]:
    tok = tok.strip()
    if tok.startswith("(") and tok.endswith(")"):
        tok = tok[1:-1]
    out = []
    for part in tok.split(","):
        ref = _parse_ref(part)
        if ref is not None:
            out.append(ref)
    return out


def _unquote(tok: str) -> str:
    tok = tok.strip()
    if len(tok) >= 2 and tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1].replace("''", "'")
    return tok


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(v: tuple[float, float, float]) -> tuple[float, float, float]:
    mag = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if mag <= 1e-12:
        return (1.0, 0.0, 0.0)
    return (v[0] / mag, v[1] / mag, v[2] / mag)


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _mat4_identity() -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _mat4_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    out = [[0.0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            out[i][j] = sum(a[i][k] * b[k][j] for k in range(4))
    return out


def _mat4_apply(m: list[list[float]], p: tuple[float, float, float]) -> tuple[float, float, float]:
    x = m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3]
    y = m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3]
    z = m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3]
    return (x, y, z)


@dataclasses.dataclass(frozen=True)
class AABB3D:
    name: str
    service: str
    ifc_type: str
    xmin: float
    ymin: float
    zmin: float
    xmax: float
    ymax: float
    zmax: float
    center: tuple[float, float, float]
    axis_endpoints: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None

    @property
    def volume(self) -> float:
        return max(0.0, self.xmax - self.xmin) * max(0.0, self.ymax - self.ymin) * max(0.0, self.zmax - self.zmin)


@dataclasses.dataclass
class IFC4AuditResult:
    ok: bool
    elements: list[AABB3D]
    cross_service_clashes: list[dict[str, Any]]
    same_service_clashes: list[dict[str, Any]]
    clearance_violations: list[dict[str, Any]]
    unserved_endpoints: list[str]
    forbidden_zone_violations: list[dict[str, Any]]
    empty_geometry_elements: list[str]
    min_cross_clearance: float | None
    total_route_length: float
    defects: list[dict[str, Any]]


def _parse_step_entities(step_text: str) -> dict[int, tuple[str, list[str]]]:
    entities: dict[int, tuple[str, list[str]]] = {}
    # Split statements safely line-by-line or across DATA section
    data_start = step_text.find("DATA;")
    body = step_text[data_start + 5 :] if data_start != -1 else step_text
    # Rejoin multi-line statements
    stmt_buf: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("/*"):
            continue
        stmt_buf.append(line)
        if line.endswith(";"):
            stmt = " ".join(stmt_buf)
            stmt_buf = []
            m = _ENTITY_RE.match(stmt)
            if m:
                eid = int(m.group(1))
                etype = m.group(2).upper()
                args = _split_step_args(m.group(3))
                entities[eid] = (etype, args)
    return entities


def _axis2_placement_3d(eid: int | None, entities: dict[int, tuple[str, list[str]]]) -> list[list[float]]:
    if eid is None or eid not in entities:
        return _mat4_identity()
    etype, args = entities[eid]
    if etype == "IFCAXIS2PLACEMENT3D":
        loc_id = _parse_ref(args[0]) if len(args) > 0 else None
        axis_id = _parse_ref(args[1]) if len(args) > 1 else None
        ref_id = _parse_ref(args[2]) if len(args) > 2 else None
        loc = (0.0, 0.0, 0.0)
        if loc_id and loc_id in entities and entities[loc_id][0] == "IFCCARTESIANPOINT":
            pts = _parse_float_tuple(entities[loc_id][1][0])
            loc = (pts[0], pts[1], pts[2] if len(pts) > 2 else 0.0)
        z_dir = (0.0, 0.0, 1.0)
        if axis_id and axis_id in entities and entities[axis_id][0] == "IFCDIRECTION":
            d = _parse_float_tuple(entities[axis_id][1][0])
            z_dir = _norm((d[0], d[1], d[2] if len(d) > 2 else 0.0))
        x_raw = (1.0, 0.0, 0.0)
        if ref_id and ref_id in entities and entities[ref_id][0] == "IFCDIRECTION":
            d = _parse_float_tuple(entities[ref_id][1][0])
            x_raw = (d[0], d[1], d[2] if len(d) > 2 else 0.0)
        # Gram-Schmidt orthogonalization
        proj = _dot(x_raw, z_dir)
        x_orth = (x_raw[0] - proj * z_dir[0], x_raw[1] - proj * z_dir[1], x_raw[2] - proj * z_dir[2])
        if math.sqrt(_dot(x_orth, x_orth)) < 1e-9:
            fallback = (0.0, 1.0, 0.0) if abs(z_dir[0]) > 0.9 else (1.0, 0.0, 0.0)
            p2 = _dot(fallback, z_dir)
            x_orth = (fallback[0] - p2 * z_dir[0], fallback[1] - p2 * z_dir[1], fallback[2] - p2 * z_dir[2])
        x_dir = _norm(x_orth)
        y_dir = _norm(_cross(z_dir, x_dir))
        return [
            [x_dir[0], y_dir[0], z_dir[0], loc[0]],
            [x_dir[1], y_dir[1], z_dir[1], loc[1]],
            [x_dir[2], y_dir[2], z_dir[2], loc[2]],
            [0.0, 0.0, 0.0, 1.0],
        ]
    if etype == "IFCAXIS2PLACEMENT2D":
        loc_id = _parse_ref(args[0]) if len(args) > 0 else None
        ref_id = _parse_ref(args[1]) if len(args) > 1 else None
        loc = (0.0, 0.0, 0.0)
        if loc_id and loc_id in entities and entities[loc_id][0] == "IFCCARTESIANPOINT":
            pts = _parse_float_tuple(entities[loc_id][1][0])
            loc = (pts[0], pts[1], 0.0)
        x_dir = (1.0, 0.0, 0.0)
        if ref_id and ref_id in entities and entities[ref_id][0] == "IFCDIRECTION":
            d = _parse_float_tuple(entities[ref_id][1][0])
            x_dir = _norm((d[0], d[1], 0.0))
        y_dir = (-x_dir[1], x_dir[0], 0.0)
        return [
            [x_dir[0], y_dir[0], 0.0, loc[0]],
            [x_dir[1], y_dir[1], 0.0, loc[1]],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    return _mat4_identity()


def _compose_local_placement(pid: int | None, entities: dict[int, tuple[str, list[str]]], depth: int = 0) -> list[list[float]]:
    if depth > 32 or pid is None or pid not in entities:
        return _mat4_identity()
    etype, args = entities[pid]
    if etype != "IFCLOCALPLACEMENT":
        return _mat4_identity()
    parent_id = _parse_ref(args[0]) if len(args) > 0 else None
    rel_id = _parse_ref(args[1]) if len(args) > 1 else None
    parent_m = _compose_local_placement(parent_id, entities, depth + 1)
    rel_m = _axis2_placement_3d(rel_id, entities)
    return _mat4_mul(parent_m, rel_m)


def _local_solid_corners(
    geom_id: int, entities: dict[int, tuple[str, list[str]]]
) -> tuple[list[tuple[float, float, float]], tuple[tuple[float, float, float], tuple[float, float, float]] | None]:
    """Extracts 8 local bounding corners (and optional axis endpoints) for ExtrudedAreaSolid or BoundingBox."""
    if geom_id not in entities:
        return [], None
    etype, args = entities[geom_id]
    if etype == "IFCEXTRUDEDAREASOLID":
        prof_id = _parse_ref(args[0]) if len(args) > 0 else None
        pos_id = _parse_ref(args[1]) if len(args) > 1 else None
        dir_id = _parse_ref(args[2]) if len(args) > 2 else None
        depth = float(args[3]) if len(args) > 3 else 0.0
        if depth <= 0 or prof_id is None or prof_id not in entities:
            return [], None

        ext_dir = (0.0, 0.0, 1.0)
        if dir_id and dir_id in entities and entities[dir_id][0] == "IFCDIRECTION":
            d = _parse_float_tuple(entities[dir_id][1][0])
            ext_dir = _norm((d[0], d[1], d[2] if len(d) > 2 else 0.0))

        ptype, pargs = entities[prof_id]
        xs: list[float] = []
        ys: list[float] = []
        p_m = _mat4_identity()
        if ptype == "IFCRECTANGLEPROFILEDEF":
            # (ProfileType, ProfileName, Position, XDim, YDim)
            p_pos = _parse_ref(pargs[2]) if len(pargs) > 2 else None
            p_m = _axis2_placement_3d(p_pos, entities)
            xd = float(pargs[3]) / 2.0
            yd = float(pargs[4]) / 2.0
            xs, ys = [-xd, xd], [-yd, yd]
        elif ptype == "IFCCIRCLEPROFILEDEF":
            p_pos = _parse_ref(pargs[2]) if len(pargs) > 2 else None
            p_m = _axis2_placement_3d(p_pos, entities)
            r = float(pargs[3])
            xs, ys = [-r, r], [-r, r]
        elif ptype == "IFCARBITRARYCLOSEDPROFILEDEF":
            curve_id = _parse_ref(pargs[2]) if len(pargs) > 2 else None
            if curve_id and curve_id in entities and entities[curve_id][0] == "IFCPOLYLINE":
                pt_ids = _parse_ref_list(entities[curve_id][1][0])
                for pt_id in pt_ids:
                    if pt_id in entities:
                        c = _parse_float_tuple(entities[pt_id][1][0])
                        xs.append(c[0])
                        ys.append(c[1])
        if not xs or not ys:
            return [], None

        solid_m = _mat4_mul(_axis2_placement_3d(pos_id, entities), p_m)
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        corners = []
        for x in (minx, maxx):
            for y in (miny, maxy):
                for t in (0.0, depth):
                    pt = (x + t * ext_dir[0], y + t * ext_dir[1], t * ext_dir[2])
                    corners.append(_mat4_apply(solid_m, pt))
        ep0 = _mat4_apply(solid_m, (cx, cy, 0.0))
        ep1 = _mat4_apply(solid_m, (cx + depth * ext_dir[0], cy + depth * ext_dir[1], depth * ext_dir[2]))
        return corners, (ep0, ep1)

    if etype == "IFCBOUNDINGBOX":
        # (Corner, XDim, YDim, ZDim)
        c_id = _parse_ref(args[0])
        dx, dy, dz = float(args[1]), float(args[2]), float(args[3])
        if dx <= 0 or dy <= 0 or dz <= 0 or not c_id or c_id not in entities:
            return [], None
        base = _parse_float_tuple(entities[c_id][1][0])
        bx, by, bz = base[0], base[1], base[2] if len(base) > 2 else 0.0
        corners = [
            (bx + ox, by + oy, bz + oz)
            for ox in (0.0, dx)
            for oy in (0.0, dy)
            for oz in (0.0, dz)
        ]
        return corners, ((bx, by + dy / 2.0, bz + dz / 2.0), (bx + dx, by + dy / 2.0, bz + dz / 2.0))

    return [], None


def _aabb_gap(a: AABB3D, b: AABB3D) -> tuple[float, float, float]:
    gx = max(0.0, a.xmin - b.xmax, b.xmin - a.xmax)
    gy = max(0.0, a.ymin - b.ymax, b.ymin - a.ymax)
    gz = max(0.0, a.zmin - b.zmax, b.zmin - a.zmax)
    return gx, gy, gz


def _aabb_overlap_depth(a: AABB3D, b: AABB3D) -> float:
    ox = min(a.xmax, b.xmax) - max(a.xmin, b.xmin)
    oy = min(a.ymax, b.ymax) - max(a.ymin, b.ymin)
    oz = min(a.zmax, b.zmax) - max(a.zmin, b.zmin)
    if ox <= 0.0 or oy <= 0.0 or oz <= 0.0:
        return 0.0
    return min(ox, oy, oz)


def _aabb_distance(a: AABB3D, b: AABB3D) -> float:
    gx, gy, gz = _aabb_gap(a, b)
    return math.sqrt(gx * gx + gy * gy + gz * gz)


def _pt_dist(p: tuple[float, float, float], q: tuple[float, float, float]) -> float:
    return math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2)


def audit_ifc4_bytes(
    ifc_bytes: bytes,
    *,
    required_endpoints: dict[str, list[tuple[float, float, float]]] | None = None,
    source_points: dict[str, tuple[float, float, float]] | None = None,
    forbidden_zones: list[dict[str, Any]] | None = None,
    min_clearance: float = 0.05,
    same_service_overlap_tol: float = 0.002,
    connectivity_tol: float = 0.30,
    service_rules: dict[str, str] | None = None,
) -> IFC4AuditResult:
    """Independently reads STEP IFC4 placements + solids and verifies clashes, clearances, zones, and reachability.

    Args:
        ifc_bytes: Raw UTF-8 IFC4 STEP file bytes.
        required_endpoints: Mapping `{service_name: [(x,y,z), ...]}` of target points that must be reached.
        source_points: Optional mapping `{service_name: (x,y,z)}` for BFS reachability from source.
        forbidden_zones: List of dicts `{"name": str, "xmin": ..., "xmax": ..., "ymin": ..., "ymax": ..., "zmin": ..., "zmax": ..., "services": [...]}`
            representing obstacles or disallowed corridors/aisles.
        min_clearance: Minimum required cross-service clearance (in IFC units, e.g. 0.05 m = 50 mm).
        same_service_overlap_tol: Max allowed penetration between two elements of the SAME service before flagging corner self-clash.
        connectivity_tol: Max gap between adjacent route segment endpoints (or segment AABB to target point).
        service_rules: Optional prefix/substring -> service tag overrides.
    """
    text = ifc_bytes.decode("utf-8", errors="replace")
    entities = _parse_step_entities(text)

    # Map Object -> System/Service name via IfcRelAssignsToGroup if present
    group_service: dict[int, str] = {}
    for _, (etype, args) in entities.items():
        if etype == "IFCRELASSIGNSTOGROUP" and len(args) >= 7:
            grp_id = _parse_ref(args[6])
            svc_name = ""
            if grp_id and grp_id in entities:
                svc_name = _unquote(entities[grp_id][1][2]) if len(entities[grp_id][1]) > 2 else ""
            if svc_name:
                for obj_id in _parse_ref_list(args[4]):
                    group_service[obj_id] = svc_name

    def infer_service(eid: int, name: str, etype: str) -> str:
        if eid in group_service:
            return group_service[eid]
        if service_rules:
            for prefix, svc in service_rules.items():
                if prefix.lower() in name.lower():
                    return svc
        low = name.lower()
        if ":" in name:
            return name.split(":", 1)[0].strip()
        if any(k in low for k in ("cable", "tray", "ct-")):
            return "cable_tray"
        if any(k in low for k in ("bus", "bw-")):
            return "busway"
        if any(k in low for k in ("pipe", "chw", "pcw")):
            return "chilled_water"
        if any(k in low for k in ("sprink", "fp-")):
            return "sprinkler"
        if any(k in low for k in ("col", "beam", "slab", "wall", "struct", "aisle", "obs")):
            return "structure"
        return etype.lower()

    elements: list[AABB3D] = []
    empty_geom: list[str] = []

    PRODUCT_TYPES = (
        "IFCCABLECARRIERSEGMENT",
        "IFCPIPESEGMENT",
        "IFCDUCTSEGMENT",
        "IFCFLOWSEGMENT",
        "IFCFLOWFITTING",
        "IFCBUILDINGELEMENTPROXY",
        "IFCCOLUMN",
        "IFCBEAM",
        "IFCSLAB",
        "IFCWALL",
        "IFCWALLSTANDARDCASE",
        "IFCFURNISHINGELEMENT",
    )

    for eid, (etype, args) in entities.items():
        if etype not in PRODUCT_TYPES:
            continue
        # Standard IfcProduct: (GlobalId, OwnerHistory, Name, Description, ObjectType, ObjectPlacement, Representation, ...)
        name = _unquote(args[2]) if len(args) > 2 and args[2] != "$" else f"{etype}_{eid}"
        place_id = _parse_ref(args[5]) if len(args) > 5 else None
        repr_id = _parse_ref(args[6]) if len(args) > 6 else None
        service = infer_service(eid, name, etype)

        world_m = _compose_local_placement(place_id, entities)
        pts_world: list[tuple[float, float, float]] = []
        ep_world: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None

        if repr_id and repr_id in entities and entities[repr_id][0] == "IFCPRODUCTDEFINITIONSHAPE":
            rep_ids = _parse_ref_list(entities[repr_id][1][2]) if len(entities[repr_id][1]) > 2 else []
            for r_id in rep_ids:
                if r_id in entities and entities[r_id][0] == "IFCSHAPEREPRESENTATION":
                    item_ids = _parse_ref_list(entities[r_id][1][3]) if len(entities[r_id][1]) > 3 else []
                    for item_id in item_ids:
                        corners, eps = _local_solid_corners(item_id, entities)
                        for pt in corners:
                            pts_world.append(_mat4_apply(world_m, pt))
                        if eps and ep_world is None:
                            ep_world = (_mat4_apply(world_m, eps[0]), _mat4_apply(world_m, eps[1]))

        if not pts_world:
            empty_geom.append(name)
            continue

        xs = [p[0] for p in pts_world]
        ys = [p[1] for p in pts_world]
        zs = [p[2] for p in pts_world]
        aabb = AABB3D(
            name=name,
            service=service,
            ifc_type=etype,
            xmin=min(xs),
            ymin=min(ys),
            zmin=min(zs),
            xmax=max(xs),
            ymax=max(ys),
            zmax=max(zs),
            center=((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, (min(zs) + max(zs)) / 2.0),
            axis_endpoints=ep_world,
        )
        if aabb.volume <= 1e-12:
            empty_geom.append(name)
            continue
        elements.append(aabb)

    cross_clashes: list[dict[str, Any]] = []
    same_clashes: list[dict[str, Any]] = []
    clearance_viols: list[dict[str, Any]] = []
    min_cross_clear: float | None = None

    for i in range(len(elements)):
        for j in range(i + 1, len(elements)):
            a, b = elements[i], elements[j]
            pen = _aabb_overlap_depth(a, b)
            dist = _aabb_distance(a, b)
            if a.service == b.service:
                if pen > same_service_overlap_tol:
                    same_clashes.append(
                        {
                            "a": a.name,
                            "b": b.name,
                            "service": a.service,
                            "penetration": round(pen, 5),
                        }
                    )
            else:
                if min_cross_clear is None or dist < min_cross_clear:
                    min_cross_clear = dist
                if pen > 1e-6:
                    cross_clashes.append(
                        {
                            "a": a.name,
                            "service_a": a.service,
                            "b": b.name,
                            "service_b": b.service,
                            "penetration": round(pen, 5),
                        }
                    )
                elif dist < min_clearance - 1e-6:
                    clearance_viols.append(
                        {
                            "a": a.name,
                            "service_a": a.service,
                            "b": b.name,
                            "service_b": b.service,
                            "clearance": round(dist, 5),
                            "required": min_clearance,
                        }
                    )

    # Forbidden zones / aisle constraints
    zone_viols: list[dict[str, Any]] = []
    for z in forbidden_zones or []:
        z_box = AABB3D(
            name=str(z.get("name", "zone")),
            service="zone",
            ifc_type="ZONE",
            xmin=float(z["xmin"]),
            ymin=float(z["ymin"]),
            zmin=float(z["zmin"]),
            xmax=float(z["xmax"]),
            ymax=float(z["ymax"]),
            zmax=float(z["zmax"]),
            center=(0.0, 0.0, 0.0),
        )
        restricted = set(z.get("services") or [])
        for el in elements:
            if restricted and el.service not in restricted:
                continue
            if el.service == "structure":
                continue
            pen = _aabb_overlap_depth(el, z_box)
            if pen > 1e-6:
                zone_viols.append({"element": el.name, "service": el.service, "zone": z_box.name, "penetration": round(pen, 5)})

    # Endpoint reachability & connectivity per service
    unserved: list[str] = []
    by_service: dict[str, list[AABB3D]] = {}
    for el in elements:
        by_service.setdefault(el.service, []).append(el)

    def point_to_box_dist(pt: tuple[float, float, float], box: AABB3D) -> float:
        dx = max(0.0, box.xmin - pt[0], pt[0] - box.xmax)
        dy = max(0.0, box.ymin - pt[1], pt[1] - box.ymax)
        dz = max(0.0, box.zmin - pt[2], pt[2] - box.zmax)
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    for svc, targets in (required_endpoints or {}).items():
        segs = by_service.get(svc, [])
        if not segs:
            for idx, pt in enumerate(targets):
                unserved.append(f"{svc}:target_{idx}@{pt}")
            continue
        # If source_point provided, BFS across touching segment boxes
        if source_points and svc in source_points:
            src_pt = source_points[svc]
            visited: set[int] = set()
            q: deque[int] = deque()
            for idx, s in enumerate(segs):
                if point_to_box_dist(src_pt, s) <= connectivity_tol:
                    visited.add(idx)
                    q.append(idx)
            while q:
                cur = q.popleft()
                for nxt in range(len(segs)):
                    if nxt not in visited and _aabb_distance(segs[cur], segs[nxt]) <= connectivity_tol:
                        visited.add(nxt)
                        q.append(nxt)
            reachable_segs = [segs[i] for i in visited]
        else:
            reachable_segs = segs

        for idx, pt in enumerate(targets):
            if not any(point_to_box_dist(pt, s) <= connectivity_tol for s in reachable_segs):
                unserved.append(f"{svc}:target_{idx}@{pt}")

    # Compute total route length across routed MEP elements
    total_len = 0.0
    for el in elements:
        if el.service == "structure":
            continue
        if el.axis_endpoints is not None:
            total_len += _pt_dist(el.axis_endpoints[0], el.axis_endpoints[1])
        else:
            total_len += max(el.xmax - el.xmin, el.ymax - el.ymin, el.zmax - el.zmin)

    defects: list[dict[str, Any]] = []
    if empty_geom:
        defects.append(
            {
                "item": "empty_ifc_geometry",
                "requirement_source": "IFC4 non-empty product geometry",
                "measured": empty_geom,
                "limit": 0,
                "tolerance": 0,
                "earliest_suspect_dependency": f"Solid extrusion/representation for {empty_geom[0]}",
            }
        )
    for cl in cross_clashes:
        defects.append(
            {
                "item": f"{cl['a']} vs {cl['b']}",
                "requirement_source": "Hard cross-service 3D clash check",
                "measured": f"overlap={cl['penetration']}",
                "limit": "0.0",
                "tolerance": 0.0,
                "earliest_suspect_dependency": f"Elevation/path of {cl['a']} or {cl['b']}",
            }
        )
    for sc in same_clashes:
        defects.append(
            {
                "item": f"{sc['a']} vs {sc['b']}",
                "requirement_source": "Same-service 3D corner self-clash check",
                "measured": f"overlap={sc['penetration']}",
                "limit": f"<= {same_service_overlap_tol}",
                "tolerance": same_service_overlap_tol,
                "earliest_suspect_dependency": f"Trim centerline extension of {sc['b']} by half-width + gap",
            }
        )
    for cv in clearance_viols:
        defects.append(
            {
                "item": f"{cv['a']} <-> {cv['b']}",
                "requirement_source": "Minimum 3D service clearance",
                "measured": cv["clearance"],
                "limit": min_clearance,
                "tolerance": 0.0,
                "earliest_suspect_dependency": f"Vertical/lateral offset between {cv['a']} and {cv['b']}",
            }
        )
    for zv in zone_viols:
        defects.append(
            {
                "item": f"{zv['element']} in {zv['zone']}",
                "requirement_source": "Project corridor/aisle zone constraint",
                "measured": f"penetration={zv['penetration']}",
                "limit": "0.0",
                "tolerance": 0.0,
                "earliest_suspect_dependency": f"Routing through forbidden zone {zv['zone']}",
            }
        )
    for ue in unserved:
        defects.append(
            {
                "item": ue,
                "requirement_source": "Required service point reachability",
                "measured": "disconnected",
                "limit": f"<= {connectivity_tol}",
                "tolerance": connectivity_tol,
                "earliest_suspect_dependency": f"Branch connection / continuity for {ue}",
            }
        )

    project_entities = [args for _, (et, args) in entities.items() if et == "IFCPROJECT"]
    if project_entities:
        has_geom_ctx = any(
            et in ("IFCGEOMETRICREPRESENTATIONCONTEXT", "IFCGEOMETRICREPRESENTATIONSUBCONTEXT")
            for _, (et, _) in entities.items()
        )
        proj_has_ctx = any(
            len(args) > 7 and bool(_parse_ref_list(args[7]))
            for args in project_entities
        )
        if not has_geom_ctx or not proj_has_ctx:
            defects.append(
                {
                    "item": "missing_representation_contexts",
                    "requirement_source": "IFC4 schema: IfcProject.RepresentationContexts required for geometry validation",
                    "measured": "missing" if not has_geom_ctx else "unlinked on IfcProject",
                    "limit": "non-empty IfcProject.RepresentationContexts referencing IfcGeometricRepresentationContext",
                    "tolerance": 0,
                    "earliest_suspect_dependency": "Create an IfcGeometricRepresentationContext ('Model', 3D) and attach it to IfcProject.RepresentationContexts (e.g., via ifcopenshell.api.context.add_context)",
                }
            )
        has_spatial = any(
            et in ("IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY")
            for _, (et, _) in entities.items()
        )
        if not has_spatial:
            defects.append(
                {
                    "item": "missing_spatial_structure",
                    "requirement_source": "IFC4 spatial hierarchy: IfcSite/IfcBuilding/IfcBuildingStorey required",
                    "measured": "none",
                    "limit": ">= 1 spatial container",
                    "tolerance": 0,
                    "earliest_suspect_dependency": "Create IfcSite / IfcBuilding / IfcBuildingStorey and assign elements via ifcopenshell.api.spatial.assign_container",
                }
            )

    return IFC4AuditResult(
        ok=len(defects) == 0,
        elements=elements,
        cross_service_clashes=cross_clashes,
        same_service_clashes=same_clashes,
        clearance_violations=clearance_viols,
        unserved_endpoints=unserved,
        forbidden_zone_violations=zone_viols,
        empty_geometry_elements=empty_geom,
        min_cross_clearance=min_cross_clear,
        total_route_length=round(total_len, 6),
        defects=defects,
    )


def translate_ifc4_step(ifc_bytes: bytes, dx: float, dy: float, dz: float) -> bytes:
    """Translates top-level root IfcLocalPlacement origins by `(dx, dy, dz)` without mutating shared local points."""
    text = ifc_bytes.decode("utf-8", errors="replace")
    entities = _parse_step_entities(text)

    # Map root IFCAXIS2PLACEMENT3D -> new IFCCARTESIANPOINT ID so shared origin points in profiles aren't shifted
    next_eid = max(entities.keys(), default=0) + 1
    axis_to_new_pt: dict[int, tuple[int, str]] = {}
    for _, (etype, args) in entities.items():
        if etype == "IFCLOCALPLACEMENT" and len(args) >= 2 and args[0].strip() == "$":
            axis_id = _parse_ref(args[1])
            if axis_id and axis_id not in axis_to_new_pt and axis_id in entities:
                a_type, a_args = entities[axis_id]
                loc_id = _parse_ref(a_args[0]) if a_args else None
                if a_type == "IFCAXIS2PLACEMENT3D" and loc_id and loc_id in entities:
                    pts = _parse_float_tuple(entities[loc_id][1][0])
                    nx = pts[0] + dx
                    ny = pts[1] + dy
                    nz = (pts[2] if len(pts) > 2 else 0.0) + dz
                    axis_to_new_pt[axis_id] = (
                        next_eid,
                        f"#{next_eid}=IFCCARTESIANPOINT(({nx:.6f},{ny:.6f},{nz:.6f}));",
                    )
                    next_eid += 1

    if not axis_to_new_pt:
        return ifc_bytes

    def repl_line(line: str) -> str:
        m = _ENTITY_RE.match(line.strip())
        if not m:
            return line
        eid = int(m.group(1))
        if eid in axis_to_new_pt:
            args = _split_step_args(m.group(3))
            new_pt_id, _ = axis_to_new_pt[eid]
            args[0] = f"#{new_pt_id}"
            return f"#{eid}={m.group(2).upper()}({','.join(args)});"
        return line

    out_lines: list[str] = []
    in_data = False
    inserted = False
    new_pt_lines = [line for _, line in axis_to_new_pt.values()]
    for ln in text.splitlines():
        upper_ln = ln.strip().upper()
        if upper_ln == "DATA;":
            in_data = True
        elif in_data and not inserted and upper_ln == "ENDSEC;":
            out_lines.extend(new_pt_lines)
            inserted = True
        out_lines.append(repl_line(ln))
    if not inserted:
        out_lines.extend(new_pt_lines)
    return ("\n".join(out_lines) + "\n").encode("utf-8")
