"""常時 RTT モニタ.

スループット測定とは独立に UDP エコープローブを流し続け、集計窓
(既定 1 秒) ごとに RTT / ジッタ / ロスを 1 行の NetSample として記録する。
瞬断・ハンドオーバー時の秒単位の挙動を捉えるための機能。

- プローブは既定 200ms 間隔 (1 窓あたり 5 プローブ)。帯域負荷は
  1200B × 5/s ≒ 50kbps 程度で、実運用への影響は無視できる。
- 記録は direction="rtt" の NetSample (throughput 系列とは区別される)。
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Dict

from ..model import NetSample
from .protocol import UDP_PAYLOAD, encode_probe, decode_probe

log = logging.getLogger("bdp.netqual.rttmon")

_FINALIZE_GRACE_S = 0.4   # 窓を閉じる前に遅延到着を待つ時間


def run_monitor(*, host: str, udp_port: int, stop: threading.Event,
                on_sample: Callable[[NetSample], None], session: str,
                probe_interval_ms: float = 200.0,
                agg_seconds: float = 1.0,
                bind_ip: str | None = None) -> None:
    """stop がセットされるまで RTT を監視し続ける (専用スレッドで実行する).

    bind_ip 指定時は送信元をそのアダプタ IP に固定する (マルチ NIC 用)。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if bind_ip:
            sock.bind((bind_ip, 0))
        sock.connect((host, udp_port))
    except OSError as e:
        log.error("[%s] RTT モニタ開始失敗: %s", session, e)
        return

    interval = max(0.05, probe_interval_ms / 1000.0)
    agg = max(1.0, float(agg_seconds))
    windows: Dict[int, Dict] = {}    # 窓番号 -> {sent, rtts[]}
    sent_seq: Dict[int, int] = {}    # seq -> 窓番号
    seq = 0
    next_send = time.monotonic()

    def finalize(widx: int) -> None:
        w = windows.pop(widx, None)
        if not w:
            return
        rtts = w["rtts"]
        jitter = 0.0
        for i in range(1, len(rtts)):
            jitter += (abs(rtts[i] - rtts[i - 1]) - jitter) / 16.0
        on_sample(NetSample(
            ts=(widx + 1) * agg,          # 窓の終端時刻
            session=session,
            direction="rtt",
            rtt_ms=round(sum(rtts) / len(rtts), 3) if rtts else None,
            rtt_min_ms=round(min(rtts), 3) if rtts else None,
            rtt_max_ms=round(max(rtts), 3) if rtts else None,
            jitter_ms=round(jitter, 3) if rtts else None,
            loss_pct=round((w["sent"] - len(rtts)) / w["sent"] * 100.0, 3)
                     if w["sent"] else None,
        ))

    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_send:
                widx = int(time.time() // agg)
                try:
                    sock.send(encode_probe(seq, time.time_ns()))
                    windows.setdefault(widx, {"sent": 0, "rtts": []})["sent"] += 1
                    sent_seq[seq] = widx
                except OSError:
                    pass
                seq += 1
                next_send += interval
                if next_send < now - 1.0:
                    next_send = now

            # 次の送出時刻まで応答を回収
            wait = max(0.005, min(next_send - time.monotonic(), interval))
            sock.settimeout(wait)
            try:
                data = sock.recv(UDP_PAYLOAD)
                rseq, t0 = decode_probe(data)
                rtt = (time.time_ns() - t0) / 1e6
                widx = sent_seq.pop(rseq, None)
                if widx is not None and widx in windows and rtt >= 0:
                    windows[widx]["rtts"].append(rtt)
            except socket.timeout:
                pass
            except (OSError, ValueError, UnicodeDecodeError):
                pass

            # 締め切りを過ぎた窓を確定
            cur_widx = int(time.time() // agg)
            for past_widx in [w for w in windows
                              if w < cur_widx and
                              time.time() > (w + 1) * agg + _FINALIZE_GRACE_S]:
                finalize(past_widx)
            # 応答が来なかった古い seq を掃除
            if len(sent_seq) > 10000:
                cutoff = cur_widx - 5
                for k in [k for k, v in sent_seq.items() if v < cutoff]:
                    del sent_seq[k]
    finally:
        for widx in list(windows):
            finalize(widx)
        sock.close()
        log.info("[%s] RTT モニタ停止", session)
