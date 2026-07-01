"""RF コレクタ群."""
from __future__ import annotations

from typing import Any, Dict

from .base import Collector
from .starlink import StarlinkCollector
from .kymeta import KymetaCollector
from .intellian import IntellianCollector

_REGISTRY = {
    "starlink": StarlinkCollector,
    "kymeta": KymetaCollector,
    "intellian": IntellianCollector,
}


def build_collector(cfg: Dict[str, Any]) -> Collector:
    kind = cfg.get("kind")
    if kind not in _REGISTRY:
        raise ValueError(f"unknown collector kind: {kind!r}")
    return _REGISTRY[kind](cfg)
