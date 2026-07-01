"""コレクタ基底クラスと共通ユーティリティ."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..model import RFSample

log = logging.getLogger("bdp.collector")


class Collector:
    """RF メトリクスを 1 点取得するコレクタの基底.

    サブクラスは :meth:`poll` を実装し、取得できなければ ``None`` を返す。
    例外は投げず、内部でログして ``None`` を返すこと(監視ループを止めない)。
    """

    kind = "base"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.name = cfg.get("name", self.kind)
        self.interval_s = float(cfg.get("interval_s", 10))

    def poll(self, now: float) -> Optional[RFSample]:
        raise NotImplementedError

    def close(self) -> None:  # 任意
        pass


def dig(obj: Any, dotted: str) -> Optional[Any]:
    """"a.b.c" 形式のパスで dict/list をたどる。見つからなければ None."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
