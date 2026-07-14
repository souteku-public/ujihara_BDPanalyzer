"""iperf3 バックエンド (スループット測定の委譲).

iperf3 が OS に入っていれば、スループット測定だけを iperf3 (JSON 出力 -J) に
委譲する。C 実装のため高速リンクでも CPU 律速になりにくく、単発スループットの
精度・実効上限が内蔵実装より高い。RTT/ジッタ/ロス・相関・記録は引き続き本体が担う。

前提: 対向 (受信側) で iperf3 サーバが動いていること。
      `iperf3 -s -p <port>` または本アプリ receiver の `--iperf-port` で起動。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Optional

log = logging.getLogger("bdp.netqual.iperf")


def available(binary: str = "iperf3") -> bool:
    return shutil.which(binary) is not None


def _run(args, timeout: float) -> Optional[dict]:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("iperf3 実行失敗: %s", e)
        return None
    # iperf3 はエラー時も JSON に "error" を載せて返すことがある
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        log.warning("iperf3 出力を JSON 解析できません: %s", (out.stderr or "")[:200])
        return None
    if data.get("error"):
        log.warning("iperf3 エラー: %s", data["error"])
        return None
    return data


def _sum_bps(data: dict, sender: bool) -> Optional[float]:
    end = data.get("end", {})
    key = "sum_sent" if sender else "sum_received"
    node = end.get(key) or end.get("sum") or {}
    bps = node.get("bits_per_second")
    return float(bps) if bps is not None else None


def throughput_bps(host: str, port: int, seconds: float, *,
                   direction: str, streams: int = 1, omit: float = 0.0,
                   bind_ip: Optional[str] = None, binary: str = "iperf3",
                   timeout: Optional[float] = None) -> Optional[float]:
    """iperf3 でスループット [bps] を測る.

    direction: "downlink" (-R, server→client) / "uplink" (client→server)
    戻り値は「実際に受信された側」の bits_per_second (実効値)。
    """
    args = [binary, "-c", host, "-p", str(int(port)), "-J",
            "-t", str(int(max(1, seconds))), "-P", str(int(max(1, streams)))]
    if omit and omit > 0:
        args += ["-O", str(int(omit))]
    if direction == "downlink":
        args.append("-R")                      # reverse: server が送信
    if bind_ip:
        args += ["-B", bind_ip]
    data = _run(args, timeout or (seconds + omit + 15))
    if data is None:
        return None
    # 受信側実効レートを採用 (reverse 時は client 受信=sum_received、
    # 通常時は server 受信=sum_received が実効値)
    return _sum_bps(data, sender=False) or _sum_bps(data, sender=True)
