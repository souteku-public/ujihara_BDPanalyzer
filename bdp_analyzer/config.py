"""設定ファイル(YAML)の読み込み."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class GroundStation:
    name: str = "station"
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_m: float = 0.0
    elevation_mask_deg: float = 20.0


@dataclass
class Config:
    raw: Dict[str, Any]
    ground_station: GroundStation
    db_path: str
    collectors: List[Dict[str, Any]] = field(default_factory=list)
    netqual: Dict[str, Any] = field(default_factory=dict)
    handover: Dict[str, Any] = field(default_factory=dict)
    webapp: Dict[str, Any] = field(default_factory=dict)

    @property
    def enabled_collectors(self) -> List[Dict[str, Any]]:
        return [c for c in self.collectors if c.get("enabled")]


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    gs_raw = raw.get("ground_station", {})
    gs = GroundStation(
        name=gs_raw.get("name", "station"),
        latitude=float(gs_raw.get("latitude", 0.0)),
        longitude=float(gs_raw.get("longitude", 0.0)),
        altitude_m=float(gs_raw.get("altitude_m", 0.0)),
        elevation_mask_deg=float(gs_raw.get("elevation_mask_deg", 20.0)),
    )

    db_path = raw.get("storage", {}).get("db_path", "data/bdp.sqlite")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    return Config(
        raw=raw,
        ground_station=gs,
        db_path=db_path,
        collectors=raw.get("collectors", []) or [],
        netqual=raw.get("netqual", {}) or {},
        handover=raw.get("handover", {}) or {},
        webapp=raw.get("webapp", {}) or {},
    )
