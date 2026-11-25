# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Task-scoped unit-aware engineering evidence & calculation ledger."""

from __future__ import annotations

import dataclasses
import math
from typing import Any


# Conversion factors to canonical SI / base engineering units: (dimension, scale, offset)
UNIT_TABLE: dict[str, tuple[str, float, float]] = {
    # Length -> meters
    "m": ("length", 1.0, 0.0),
    "mm": ("length", 1e-3, 0.0),
    "cm": ("length", 1e-2, 0.0),
    "km": ("length", 1e3, 0.0),
    "ft": ("length", 0.3048, 0.0),
    "lf": ("length", 0.3048, 0.0),
    "in": ("length", 0.0254, 0.0),
    "yd": ("length", 0.9144, 0.0),
    # Area -> m^2
    "m2": ("area", 1.0, 0.0),
    "mm2": ("area", 1e-6, 0.0),
    "ft2": ("area", 0.09290304, 0.0),
    "sf": ("area", 0.09290304, 0.0),
    "sy": ("area", 0.83612736, 0.0),
    "in2": ("area", 0.00064516, 0.0),
    "kcmil": ("area", 5.06707479e-7, 0.0),
    # Volume -> m^3
    "m3": ("volume", 1.0, 0.0),
    "cy": ("volume", 0.764554857984, 0.0),
    "bcy": ("volume", 0.764554857984, 0.0),
    "cf": ("volume", 0.028316846592, 0.0),
    "l": ("volume", 1e-3, 0.0),
    "gal": ("volume", 0.003785411784, 0.0),
    # Electrical
    "a": ("current", 1.0, 0.0),
    "ka": ("current", 1000.0, 0.0),
    "v": ("voltage", 1.0, 0.0),
    "kv": ("voltage", 1000.0, 0.0),
    "w": ("power", 1.0, 0.0),
    "kw": ("power", 1000.0, 0.0),
    "mw": ("power", 1e6, 0.0),
    "btu/hr": ("power", 0.29307107, 0.0),
    "ton_ref": ("power", 3516.85284, 0.0),
    # Pressure / Stress -> Pa
    "pa": ("pressure", 1.0, 0.0),
    "kpa": ("pressure", 1000.0, 0.0),
    "mpa": ("pressure", 1e6, 0.0),
    "psf": ("pressure", 47.88025898, 0.0),
    "ksf": ("pressure", 47880.25898, 0.0),
    "psi": ("pressure", 6894.757293, 0.0),
    "ksi": ("pressure", 6894757.293, 0.0),
    # Force -> N
    "n": ("force", 1.0, 0.0),
    "kn": ("force", 1000.0, 0.0),
    "lb": ("force", 4.448221615, 0.0),
    "lbf": ("force", 4.448221615, 0.0),
    "kip": ("force", 4448.221615, 0.0),
    # Linear force -> N/m
    "lb/ft": ("linear_force", 14.5939029, 0.0),
    "plf": ("linear_force", 14.5939029, 0.0),
    "kn/m": ("linear_force", 1000.0, 0.0),
    # Flow -> m^3/s
    "m3/s": ("flow", 1.0, 0.0),
    "l/s": ("flow", 1e-3, 0.0),
    "gpm": ("flow", 6.30901964e-5, 0.0),
    "cfm": ("flow", 4.71947443e-4, 0.0),
    # Thermal resistivity -> K*m/W (note 100 C*cm/W = 1 K*m/W)
    "k*m/w": ("thermal_res", 1.0, 0.0),
    "c*cm/w": ("thermal_res", 0.01, 0.0),
    "c-cm/w": ("thermal_res", 0.01, 0.0),
    # Temperature -> K
    "c": ("temp", 1.0, 273.15),
    "degc": ("temp", 1.0, 273.15),
    "k": ("temp", 1.0, 0.0),
    "f": ("temp", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    "degf": ("temp", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    # Dimensionless / currency / counts
    "%": ("percent", 0.01, 0.0),
    "pct": ("percent", 0.01, 0.0),
    "usd": ("usd", 1.0, 0.0),
    "$": ("usd", 1.0, 0.0),
    "ea": ("count", 1.0, 0.0),
    "mh": ("time_hr", 1.0, 0.0),
    "hr": ("time_hr", 1.0, 0.0),
    "s": ("time_s", 1.0, 0.0),
    "min": ("time_s", 60.0, 0.0),
}

CANONICAL_UNIT = {
    "length": "m",
    "area": "m2",
    "volume": "m3",
    "current": "A",
    "voltage": "V",
    "power": "W",
    "pressure": "Pa",
    "force": "N",
    "linear_force": "N/m",
    "flow": "m3/s",
    "thermal_res": "K*m/W",
    "temp": "K",
    "percent": "ratio",
    "usd": "USD",
    "count": "EA",
    "time_hr": "hr",
    "time_s": "s",
}


def normalize_unit(value: float, unit: str) -> tuple[str, float, str]:
    """Converts `(value, unit)` into `(dimension, normalized_value, canonical_unit)`."""
    u = unit.strip().lower().replace("°", "deg").replace("·", "*")
    spec = UNIT_TABLE.get(u)
    if spec is None:
        return ("raw:" + u, float(value), unit.strip())
    dim, scale, offset = spec
    return (dim, float(value) * scale + offset, CANONICAL_UNIT[dim])


def convert_unit(value: float, from_unit: str, to_unit: str) -> float:
    """Converts `value` from `from_unit` to `to_unit`, raising ValueError on mismatch."""
    d1, norm, _ = normalize_unit(value, from_unit)
    u2 = to_unit.strip().lower().replace("°", "deg").replace("·", "*")
    if u2 not in UNIT_TABLE:
        raise ValueError(f"Unsupported target unit: {to_unit!r}")
    d2, scale2, offset2 = UNIT_TABLE[u2]
    if d1 != d2:
        raise ValueError(f"Incompatible dimensions: {from_unit!r} ({d1}) vs {to_unit!r} ({d2})")
    return (norm - offset2) / scale2


@dataclasses.dataclass
class EvidenceRecord:
    key: str
    value: Any
    unit: str
    dimension: str
    normalized_value: float | None
    normalized_unit: str
    source_locator: str
    revision: str
    dependents: list[str]
    supersedes: str | None = None
    notes: str = ""


class EvidenceLedger:
    """Task-scoped ledger tracking governing engineering values and source conflicts."""

    def __init__(self) -> None:
        self.records: dict[str, list[EvidenceRecord]] = {}

    def record(
        self,
        key: str,
        value: Any,
        unit: str,
        source_locator: str,
        revision: str = "",
        dependents: list[str] | None = None,
        supersedes: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Stores a numeric or string engineering fact with provenance and conflict detection."""
        dim = "text"
        norm_val: float | None = None
        norm_unit = unit.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            dim, norm_val, norm_unit = normalize_unit(float(value), unit)
        rec = EvidenceRecord(
            key=key.strip(),
            value=value,
            unit=unit.strip(),
            dimension=dim,
            normalized_value=norm_val,
            normalized_unit=norm_unit,
            source_locator=source_locator.strip(),
            revision=revision.strip(),
            dependents=list(dependents or []),
            supersedes=supersedes,
            notes=notes,
        )
        bucket = self.records.setdefault(rec.key, [])
        bucket.append(rec)
        conflicts = self._conflicts_for(rec.key)
        return {
            "ok": True,
            "key": rec.key,
            "normalized_value": rec.normalized_value,
            "normalized_unit": rec.normalized_unit,
            "entries_for_key": len(bucket),
            "unresolved_conflict": bool(conflicts),
            "conflicts": conflicts,
        }

    def _conflicts_for(self, key: str) -> list[dict[str, Any]]:
        bucket = self.records.get(key, [])
        if len(bucket) <= 1:
            return []
        superseded_locs = {r.supersedes for r in bucket if r.supersedes}
        active = [r for r in bucket if r.source_locator not in superseded_locs]
        if len(active) <= 1:
            return []
        # Check if active numeric values disagree beyond tiny floating noise
        nums = [r.normalized_value for r in active if r.normalized_value is not None]
        if nums and (max(nums) - min(nums)) <= 1e-6 * max(1.0, abs(max(nums))):
            # Also check if all values match stringwise when non-numeric
            return []
        if not nums and len({str(r.value).strip() for r in active}) <= 1:
            return []
        return [
            {
                "value": r.value,
                "unit": r.unit,
                "normalized_value": r.normalized_value,
                "source_locator": r.source_locator,
                "revision": r.revision,
            }
            for r in active
        ]

    def unresolved_conflicts(self) -> dict[str, list[dict[str, Any]]]:
        """Returns all keys that have multiple active conflicting locators."""
        out: dict[str, list[dict[str, Any]]] = {}
        for k in self.records:
            c = self._conflicts_for(k)
            if c:
                out[k] = c
        return out

    def query(self, key: str = "") -> dict[str, Any]:
        """Returns stored records for `key`, or a summary of all keys if empty."""
        if key:
            bucket = self.records.get(key, [])
            return {
                "key": key,
                "records": [dataclasses.asdict(r) for r in bucket],
                "conflicts": self._conflicts_for(key),
            }
        return {
            "total_keys": len(self.records),
            "unresolved_conflicts": self.unresolved_conflicts(),
            "keys": {
                k: [
                    {
                        "value": r.value,
                        "unit": r.unit,
                        "source_locator": r.source_locator,
                        "revision": r.revision,
                    }
                    for r in v
                ]
                for k, v in self.records.items()
            },
        }
