"""単独端末インターネット速度測定 (受信側 PC 不要).

測定端末 1 台を DHCP 回線 (Starlink 等) に繋ぎ、公開エンドポイントに対して
「PC → インターネット」のスループットを指定時間測る。衛星回線が経路上の
ボトルネックになるのが通常なので、衛星区間の実効速度の実用的な目安になる。

- 追加インストール不要 (標準ライブラリ urllib のみ)。アウトバウンド HTTPS のみ使用。
- 既定エンドポイントは Cloudflare のオープンな計測用 URL (アカウント不要):
    down: https://speed.cloudflare.com/__down?bytes=<N>
    up  : https://speed.cloudflare.com/__up
  社内プロキシ環境では環境変数 (HTTPS_PROXY 等) を自動的に使う。

注意 (測定値の意味): これは PC→CDN の経路全体 (衛星区間 + 地上バックホール +
CDN) を測る一般的なスピードテスト方式。衛星区間のみを厳密に切り出すものでは
ないが、通常は衛星区間がボトルネックのため良い近似になる。
"""
from __future__ import annotations

import http.client
import logging
import os
import socket
import ssl
import threading
import time
import urllib.request
from typing import List, Optional

from ..model import NetSample

log = logging.getLogger("bdp.netqual.inetspeed")

DEFAULT_ENDPOINTS = {
    "down_url": "https://speed.cloudflare.com/__down?bytes=",
    "up_url": "https://speed.cloudflare.com/__up",
    "rtt_host": "speed.cloudflare.com",
    "rtt_port": 443,
    "down_bytes": 300_000_000,   # 1 リクエストあたり要求バイト数 (時間で打ち切る)
    "up_chunk": 1_000_000,       # アップロード 1 POST あたりのバイト数
}
_UA = {"User-Agent": "BDP-Analyzer/inetspeed"}


def _opener(bind_ip: Optional[str]):
    """必要なら送信元 IP を固定した opener を返す (マルチ NIC 用)."""
    if not bind_ip:
        return urllib.request.build_opener()

    class _BoundHTTPS(http.client.HTTPSConnection):
        def connect(self):
            self.sock = socket.create_connection(
                (self.host, self.port), self.timeout, source_address=(bind_ip, 0))
            ctx = self._context or ssl.create_default_context()
            self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)

    class _BoundHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection(
                (self.host, self.port), self.timeout, source_address=(bind_ip, 0))

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_BoundHTTPS, req)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_BoundHTTP, req)

    return urllib.request.build_opener(_HTTPSHandler, _HTTPHandler)


def _download_bps(ep, seconds, timeout, bind_ip) -> Optional[float]:
    opener = _opener(bind_ip)
    url = ep["down_url"] + str(int(ep["down_bytes"]))
    deadline = time.monotonic() + seconds
    total = 0
    start = time.monotonic()
    try:
        while time.monotonic() < deadline:
            req = urllib.request.Request(url, headers=_UA)
            with opener.open(req, timeout=timeout) as r:
                while time.monotonic() < deadline:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
    except Exception as e:  # noqa: BLE001
        if total == 0:
            log.warning("下り測定失敗: %s", e)
            return None
    el = time.monotonic() - start
    return (total * 8) / el if el > 0 else None


def _upload_bps(ep, seconds, timeout, bind_ip) -> Optional[float]:
    opener = _opener(bind_ip)
    body = os.urandom(int(ep["up_chunk"]))
    deadline = time.monotonic() + seconds
    total = 0
    start = time.monotonic()
    try:
        while time.monotonic() < deadline:
            req = urllib.request.Request(ep["up_url"], data=body, method="POST",
                                         headers={**_UA,
                                                  "Content-Type": "application/octet-stream"})
            with opener.open(req, timeout=timeout) as r:
                r.read()
            total += len(body)
    except Exception as e:  # noqa: BLE001
        if total == 0:
            log.warning("上り測定失敗: %s", e)
            return None
    el = time.monotonic() - start
    return (total * 8) / el if el > 0 else None


def _rtt_ms(ep, count, timeout, bind_ip) -> dict:
    host, port = ep["rtt_host"], int(ep["rtt_port"])
    src = (bind_ip, 0) if bind_ip else None
    rtts = []
    for _ in range(count):
        t = time.monotonic()
        try:
            s = socket.create_connection((host, port), timeout=timeout,
                                         source_address=src)
            s.close()
            rtts.append((time.monotonic() - t) * 1000.0)   # TCP 接続 ≒ 1 RTT
        except OSError:
            pass
        time.sleep(0.1)
    if not rtts:
        return {"rtt_ms": None, "rtt_min_ms": None, "rtt_max_ms": None}
    return {"rtt_ms": round(sum(rtts) / len(rtts), 2),
            "rtt_min_ms": round(min(rtts), 2), "rtt_max_ms": round(max(rtts), 2)}


def _parallel_sum(fn, streams, *args) -> Optional[float]:
    if streams <= 1:
        return fn(*args)
    out: List[Optional[float]] = [None] * streams
    ths = []
    for i in range(streams):
        t = threading.Thread(target=lambda idx: out.__setitem__(idx, fn(*args)),
                             args=(i,), daemon=True)
        t.start()
        ths.append(t)
    for t in ths:
        t.join()
    vals = [v for v in out if v]
    return sum(vals) if vals else None


def measure_public(*, endpoints: Optional[dict] = None, seconds: float = 10,
                   streams: int = 4, session: str = "internet",
                   bind_ip: Optional[str] = None, timeout: float = 20,
                   rtt_count: int = 5) -> List[NetSample]:
    """公開エンドポイントへの上り/下りスループットと RTT を測る.

    衛星は高 BDP のため streams=4 程度の並列で実効容量に近づく。
    """
    ep = {**DEFAULT_ENDPOINTS, **(endpoints or {})}
    rtt = _rtt_ms(ep, rtt_count, timeout, bind_ip)
    down = _parallel_sum(_download_bps, streams, ep, seconds, timeout, bind_ip)
    up = _parallel_sum(_upload_bps, streams, ep, seconds, timeout, bind_ip)
    now = time.time()
    common = dict(session=session, streams=streams, **rtt)
    return [
        NetSample(ts=now, direction="downlink", throughput_bps=down, **common),
        NetSample(ts=now, direction="uplink", throughput_bps=up, **common),
    ]
