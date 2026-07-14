"""送信側(sender)エージェント / 測定主体.

対向の receiver に対して 1 サイクルの測定を行い、:class:`NetSample` を返す。
測定内容:
  - ダウンリンク スループット (TCP, server→client バルク)
  - アップリンク スループット (TCP, client→server バルク)
  - RTT (平均/最小/最大)、ジッタ(RFC3550 相当)、パケットロス (UDP エコー)

RTT は「送信時刻をプローブに載せて往復させ、戻り時刻との差」で測るため、
2 台の時刻同期は不要。片道遅延(owd)は NTP/PTP 同期済みなら別途拡張可能。
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import List, Optional

from ..model import NetSample
from .protocol import CHUNK, UDP_PAYLOAD, encode_probe, decode_probe

log = logging.getLogger("bdp.netqual.client")

_UP_PAYLOAD = b"U" * CHUNK
_END = b"__END__"


def _src(bind_ip: Optional[str]):
    """バインド指定 (マルチ NIC で送信元アダプタを固定する場合) を返す."""
    return (bind_ip, 0) if bind_ip else None


def _tcp_downlink_bps(host: str, port: int, seconds: float, timeout: float,
                      bind_ip: Optional[str] = None,
                      omit: float = 0.0) -> Optional[float]:
    """下り 1 ストリーム。omit 秒 (TCP スロースタート) を除外して実効レート算出."""
    try:
        with socket.create_connection((host, port), timeout=timeout,
                                      source_address=_src(bind_ip)) as s:
            s.sendall(f"TP_DOWN {seconds}\n".encode())
            s.settimeout(seconds + timeout)
            total, start = 0, time.monotonic()
            omit_bytes, omit_t = None, None
            while True:
                data = s.recv(CHUNK)
                if not data:
                    break
                total += len(data)
                if omit_bytes is None and time.monotonic() - start >= omit:
                    omit_bytes, omit_t = total, time.monotonic()
            end = time.monotonic()
        if omit_bytes is not None and omit_t is not None:
            win = end - omit_t                       # 除外後の計測窓
            return ((total - omit_bytes) * 8) / win if win > 0 else None
        elapsed = end - start
        return (total * 8) / elapsed if elapsed > 0 else None
    except (OSError, socket.timeout) as e:
        log.warning("downlink 計測失敗: %s", e)
        return None


def _tcp_uplink_bps(host: str, port: int, seconds: float, timeout: float,
                    bind_ip: Optional[str] = None,
                    omit: float = 0.0) -> Optional[float]:
    """上り 1 ストリーム。omit 秒を除外した送出量から実効レートを算出."""
    try:
        with socket.create_connection((host, port), timeout=timeout,
                                      source_address=_src(bind_ip)) as s:
            s.sendall(f"TP_UP {seconds}\n".encode())
            deadline = time.monotonic() + seconds
            start = time.monotonic()
            sent = 0
            omit_bytes, omit_t = None, None
            while time.monotonic() < deadline:
                s.sendall(_UP_PAYLOAD)
                sent += len(_UP_PAYLOAD)
                if omit_bytes is None and time.monotonic() - start >= omit:
                    omit_bytes, omit_t = sent, time.monotonic()
            s.sendall(_END)
            end = time.monotonic()
            s.settimeout(timeout)
            resp = s.recv(64).decode("ascii", "replace").strip()
        # server の受信バイト数を全体の実効値の基準にしつつ、omit 窓の割合で按分
        acked = sent
        if resp.startswith("BYTES"):
            try:
                acked = int(resp.split()[1])
            except (ValueError, IndexError):
                pass
        if omit_bytes is not None and omit_t is not None and sent > 0:
            win = end - omit_t
            acked_win = acked * (sent - omit_bytes) / sent   # 送出比で按分
            return (acked_win * 8) / win if win > 0 else None
        elapsed = end - start
        return (acked * 8) / elapsed if elapsed > 0 else None
    except (OSError, socket.timeout) as e:
        log.warning("uplink 計測失敗: %s", e)
        return None


def _parallel_bps(fn, streams: int, *args) -> Optional[float]:
    """fn を streams 本並列実行し、各ストリームの実効レートを合算 (iperf -P 相当)."""
    if streams <= 1:
        return fn(*args)
    results: List[Optional[float]] = [None] * streams
    threads = []
    for i in range(streams):
        t = threading.Thread(
            target=lambda idx: results.__setitem__(idx, fn(*args)),
            args=(i,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    vals = [v for v in results if v]
    return sum(vals) if vals else None


def _udp_stats(host: str, port: int, count: int, interval_ms: float,
               timeout: float, bind_ip: Optional[str] = None) -> dict:
    """UDP エコーで RTT / jitter / loss を測る."""
    result = {"rtt_ms": None, "rtt_min_ms": None, "rtt_max_ms": None,
              "jitter_ms": None, "loss_pct": None, "out_of_order_pct": None}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        if bind_ip:
            s.bind((bind_ip, 0))
        s.connect((host, port))
    except OSError as e:
        log.warning("UDP ソケット作成失敗: %s", e)
        return result

    rtts: List[float] = []
    received = 0
    max_seq_seen = -1     # 順序逆転 (out-of-order) 検出用: 到着済み最大 seq
    out_of_order = 0
    interval = interval_ms / 1000.0
    try:
        for seq in range(count):
            t_send = time.time_ns()
            try:
                s.send(encode_probe(seq, t_send))
            except OSError:
                pass
            # 送出間隔を空けつつ、その間に戻ってきた応答を回収
            end_wait = time.monotonic() + interval
            while True:
                remaining = end_wait - time.monotonic()
                if remaining <= 0:
                    break
                s.settimeout(remaining)
                try:
                    data = s.recv(UDP_PAYLOAD)
                except socket.timeout:
                    break
                except OSError:
                    break
                try:
                    rseq, t0 = decode_probe(data)
                except (ValueError, UnicodeDecodeError):
                    continue
                rtt_ms = (time.time_ns() - t0) / 1e6
                if rtt_ms >= 0:
                    rtts.append(rtt_ms)
                    received += 1
                    # 到着順が seq の昇順から外れたら順序逆転としてカウント
                    if rseq < max_seq_seen:
                        out_of_order += 1
                    else:
                        max_seq_seen = rseq
    finally:
        s.close()

    if count > 0:
        result["loss_pct"] = round((count - received) / count * 100.0, 3)
    if received > 0:
        result["out_of_order_pct"] = round(out_of_order / received * 100.0, 3)
    if rtts:
        result["rtt_ms"] = round(sum(rtts) / len(rtts), 3)
        result["rtt_min_ms"] = round(min(rtts), 3)
        result["rtt_max_ms"] = round(max(rtts), 3)
        # RFC3550 相当のジッタ (RTT 列の平滑化偏差)
        jitter = 0.0
        for i in range(1, len(rtts)):
            d = abs(rtts[i] - rtts[i - 1])
            jitter += (d - jitter) / 16.0
        result["jitter_ms"] = round(jitter, 3)
    return result


def _throughput(host, control_port, seconds, timeout, bind_ip, streams, omit,
                direction, engine, iperf_port, iperf_bin):
    """スループット [bps]。engine='iperf3' かつ利用可能なら iperf3 に委譲、
    それ以外/失敗時は内蔵 TCP 実装にフォールバックする."""
    if engine == "iperf3":
        from . import iperf
        if iperf.available(iperf_bin):
            bps = iperf.throughput_bps(
                host, iperf_port, seconds, direction=direction,
                streams=streams, omit=omit, bind_ip=bind_ip, binary=iperf_bin)
            if bps is not None:
                return bps
            log.warning("iperf3 委譲に失敗、内蔵測定にフォールバック (%s)", direction)
        else:
            log.warning("engine=iperf3 だが iperf3 が見つからないため内蔵測定を使用")
    fn = _tcp_downlink_bps if direction == "downlink" else _tcp_uplink_bps
    return _parallel_bps(fn, streams, host, control_port, seconds, timeout,
                         bind_ip, omit)


def measure_once(host: str, control_port: int, udp_port: int, *,
                 throughput_seconds: float = 5, udp_probe_count: int = 200,
                 udp_probe_interval_ms: float = 20, timeout: float = 8,
                 session: Optional[str] = None,
                 bind_ip: Optional[str] = None,
                 streams: int = 1, omit_seconds: float = 0.0,
                 engine: str = "builtin", iperf_port: int = 5201,
                 iperf_bin: str = "iperf3") -> List[NetSample]:
    """1 サイクル測定し、downlink/uplink の NetSample を返す.

    bind_ip を指定すると全ソケットの送信元をそのアダプタ IP に固定する
    (マルチ NIC で回線ごとに測定を分ける用途)。
    streams>1 で並列 TCP ストリームの合算スループット (iperf -P 相当)、
    omit_seconds でスループット計測から先頭の TCP スロースタート分を除外する。
    engine="iperf3" にすると、スループットのみ iperf3 に委譲 (高速リンクで高精度、
    要: 対向で iperf3 サーバ)。RTT/ジッタ/ロス/順序逆転は常に内蔵で測定する。
    """
    session = session or host
    streams = max(1, int(streams))
    down = _throughput(host, control_port, throughput_seconds, timeout, bind_ip,
                       streams, omit_seconds, "downlink", engine, iperf_port,
                       iperf_bin)
    up = _throughput(host, control_port, throughput_seconds, timeout, bind_ip,
                     streams, omit_seconds, "uplink", engine, iperf_port,
                     iperf_bin)
    udp = _udp_stats(host, udp_port, udp_probe_count, udp_probe_interval_ms,
                     timeout, bind_ip)
    now = time.time()

    common = dict(session=session, streams=streams, **udp)
    return [
        NetSample(ts=now, direction="downlink", throughput_bps=down, **common),
        NetSample(ts=now, direction="uplink", throughput_bps=up, **common),
    ]


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="BDP Analyzer 送信側 (単発測定)")
    ap.add_argument("host")
    ap.add_argument("--control-port", type=int, default=5301)
    ap.add_argument("--udp-port", type=int, default=5302)
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--udp-count", type=int, default=200)
    args = ap.parse_args()
    samples = measure_once(args.host, args.control_port, args.udp_port,
                           throughput_seconds=args.seconds, udp_probe_count=args.udp_count)
    for s in samples:
        tp = f"{s.throughput_bps/1e6:.2f} Mbps" if s.throughput_bps else "n/a"
        print(f"[{s.direction}] throughput={tp} rtt={s.rtt_ms}ms "
              f"jitter={s.jitter_ms}ms loss={s.loss_pct}%")


if __name__ == "__main__":
    main()
