"""負荷耐性テスト (レートスイープ).

送出レートを段階的に上げながら UDP 負荷を流し、各ステップで
  - 実効レート (相手に実際に届いたレート)
  - パケットロス率
  - ジッタ
  - 負荷時 RTT (負荷と並行して通常プローブを流して測る = バッファブロート指標)
を測定する。「オファーしたレートに対してどこから損失が立ち上がるか」で
回線の耐性(実効容量と飽和時の挙動)が分かる。

UDP のペーシング送出は Python 実装のため、実用上の上限はおおむね
数百 Mbps 程度 (環境依存)。それ以上の負荷が必要な場合は将来拡張とする。
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from .protocol import (UDP_PAYLOAD, encode_probe, decode_probe, encode_punch,
                       decode_load, pace_send, LOAD_PREFIX)

log = logging.getLogger("bdp.netqual.loadtest")


def _rtt_probe_loop(host: str, udp_port: int, stop: threading.Event,
                    rtts: List[float], bind_ip: Optional[str] = None) -> None:
    """負荷と並行して 200ms おきに echo プローブを流し RTT を集める."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if bind_ip:
            s.bind((bind_ip, 0))
        s.connect((host, udp_port))
        s.settimeout(0.2)
    except OSError:
        return
    seq = 0
    try:
        while not stop.is_set():
            try:
                s.send(encode_probe(seq, time.time_ns()))
            except OSError:
                pass
            seq += 1
            try:
                data = s.recv(UDP_PAYLOAD)
                _sq, t0 = decode_probe(data)
                rtt = (time.time_ns() - t0) / 1e6
                if rtt >= 0:
                    rtts.append(rtt)
            except (socket.timeout, OSError, ValueError, UnicodeDecodeError):
                pass
            stop.wait(0.2)
    finally:
        s.close()


def _ctrl_connect(host: str, port: int, timeout: float,
                  bind_ip: Optional[str] = None):
    sock = socket.create_connection((host, port), timeout=timeout,
                                    source_address=(bind_ip, 0) if bind_ip else None)
    return sock, sock.makefile("rb")


def _step_uplink(host: str, cport: int, uport: int, rate_bps: float,
                 seconds: float, payload: int, timeout: float,
                 bind_ip: Optional[str] = None) -> Dict:
    ctrl, rf = _ctrl_connect(host, cport, timeout, bind_ip)
    try:
        ctrl.sendall(f"LD_UP {seconds}\n".encode())
        if not rf.readline().startswith(b"READY"):
            raise OSError("READY が返りません")
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if bind_ip:
            udp.bind((bind_ip, 0))
        udp.connect((host, uport))
        try:
            sent, _sbytes = pace_send(udp.send, seconds, rate_bps, payload)
        finally:
            udp.close()
        time.sleep(0.2)
        ctrl.settimeout(timeout)
        ctrl.sendall(b"LD_STAT\n")
        line = rf.readline().decode("ascii", "replace").split()
        if not line or line[0] != "LDSTAT":
            raise OSError("LDSTAT が返りません")
        count, nbytes, jitter = int(line[1]), int(line[2]), float(line[3])
    finally:
        ctrl.close()
    return {
        "achieved_bps": nbytes * 8 / seconds,
        "loss_pct": round(max(0.0, (sent - count) / sent * 100.0), 3) if sent else None,
        "jitter_ms": round(jitter, 3),
    }


def _step_downlink(host: str, cport: int, uport: int, rate_bps: float,
                   seconds: float, payload: int, timeout: float,
                   bind_ip: Optional[str] = None) -> Dict:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((bind_ip or "0.0.0.0", 0))
    udp.settimeout(0.2)
    nonce = os.urandom(8).hex()
    ctrl, rf = _ctrl_connect(host, cport, timeout, bind_ip)
    try:
        for _ in range(3):                      # NAT 穴あけ兼アドレス登録
            udp.sendto(encode_punch(nonce), (host, uport))
            time.sleep(0.05)
        ctrl.sendall(f"LD_DOWN {nonce} {seconds} {rate_bps} {payload}\n".encode())

        count = nbytes = 0
        jitter, prev_transit = 0.0, None
        end = time.monotonic() + seconds + 0.7
        while time.monotonic() < end:
            try:
                data, _addr = udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(LOAD_PREFIX):
                continue
            count += 1
            nbytes += len(data)
            try:
                _seq, t0 = decode_load(data)
            except (ValueError, UnicodeDecodeError):
                continue
            transit = (time.time_ns() - t0) / 1e6
            if prev_transit is not None:
                jitter += (abs(transit - prev_transit) - jitter) / 16.0
            prev_transit = transit

        ctrl.settimeout(timeout)
        line = rf.readline().decode("ascii", "replace").split()
        if not line or line[0] != "LDSENT":
            raise OSError(f"LDSENT が返りません: {' '.join(line)[:80]}")
        sent = int(line[1])
    finally:
        ctrl.close()
        udp.close()
    return {
        "achieved_bps": nbytes * 8 / seconds,
        "loss_pct": round(max(0.0, (sent - count) / sent * 100.0), 3) if sent else None,
        "jitter_ms": round(jitter, 3),
    }


def run_sweep(host: str, cport: int, uport: int, *, direction: str,
              rates_bps: List[float], step_seconds: float = 5.0,
              payload: int = UDP_PAYLOAD, timeout: float = 10.0,
              session: Optional[str] = None,
              bind_ip: Optional[str] = None,
              on_step: Optional[Callable[[Dict], None]] = None) -> List[Dict]:
    """レートスイープを実行し、ステップごとの結果 dict のリストを返す.

    direction: "uplink" (こちら→相手) | "downlink" (相手→こちら)
    bind_ip:   送信元アダプタ IP の固定 (マルチ NIC 用、任意)
    on_step:   各ステップ完了ごとに結果を受け取るコールバック (進捗表示用)
    """
    step_fn = _step_uplink if direction == "uplink" else _step_downlink
    results: List[Dict] = []
    for rate in rates_bps:
        stop = threading.Event()
        rtts: List[float] = []
        probe = threading.Thread(target=_rtt_probe_loop,
                                 args=(host, uport, stop, rtts, bind_ip),
                                 daemon=True)
        probe.start()
        try:
            r = step_fn(host, cport, uport, float(rate),
                        float(step_seconds), payload, timeout, bind_ip)
        except (OSError, ValueError) as e:
            log.warning("負荷ステップ失敗 (%.1f Mbps): %s", rate / 1e6, e)
            r = {"achieved_bps": None, "loss_pct": None, "jitter_ms": None,
                 "error": str(e)}
        finally:
            stop.set()
            probe.join(timeout=1)
        r.update({
            "ts": time.time(),
            "session": session or host,
            "direction": direction,
            "offered_bps": float(rate),
            "rtt_ms": round(sum(rtts) / len(rtts), 3) if rtts else None,
        })
        results.append(r)
        if on_step:
            on_step(r)
        log.info("負荷 %s %.1fMbps → 実効 %s Mbps, loss %s%%, RTT %s ms",
                 direction, rate / 1e6,
                 f"{r['achieved_bps']/1e6:.1f}" if r.get("achieved_bps") else "n/a",
                 r.get("loss_pct"), r.get("rtt_ms"))
    return results
