"""受信側(receiver)エージェント.

対向 PC で常駐し、送信側(sender)からの測定要求に応答する。
役割:
  - TCP 制御サーバ: HELLO / TP_DOWN(バルク送出) / TP_UP(バルク受信) を処理
  - UDP エコーサーバ: 受け取ったプローブをそのまま返す (RTT/jitter/loss 用)

インターネット越しに使う想定なので、control_port(TCP) と udp_port(UDP) を
NAT/ファイアウォールで受信側に転送しておくこと。

セキュリティ: 受信ポートは認証を持たないため、`allowed_sources` (CIDR リスト)
で送信元 IP を制限できる。指定した場合、リスト外からの TCP 接続は即切断、
UDP パケットは無応答で破棄する (ルータのフィルタと合わせた多層防御)。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import socketserver
import subprocess
import threading
import time

from .protocol import (CHUNK, HELLO, OK, LOAD_PREFIX, PUNCH_PREFIX,
                       decode_load, pace_send)

log = logging.getLogger("bdp.netqual.server")

_PAYLOAD = os.urandom(CHUNK)  # 送出用ダミーデータ

# ---- 送信元 IP 許可リスト (未設定なら全許可) --------------------------------
_ALLOWED_NETS: list = []


def set_allowed_sources(cidrs) -> None:
    """許可する送信元 CIDR を設定 (空/None で全許可)."""
    global _ALLOWED_NETS
    nets = []
    for c in (cidrs or []):
        try:
            nets.append(ipaddress.ip_network(str(c).strip(), strict=False))
        except ValueError:
            log.error("allowed_sources の CIDR が不正: %r (無視)", c)
    _ALLOWED_NETS = nets
    if nets:
        log.info("送信元制限: %s のみ許可", ", ".join(str(n) for n in nets))


def _src_allowed(ip: str) -> bool:
    if not _ALLOWED_NETS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _ALLOWED_NETS)


# ---- アクティビティ記録 (受信拠点ビューでの可視化用) ------------------------
_ACT_LOCK = threading.Lock()
_TCP_EVENTS: list = []          # [{ts, ip, cmd}] 直近 30 件
_UDP_COUNTS = {"echo": 0, "load": 0, "denied": 0}


def _note_tcp(ip: str, cmd: str) -> None:
    with _ACT_LOCK:
        _TCP_EVENTS.append({"ts": time.time(), "ip": ip, "cmd": cmd})
        del _TCP_EVENTS[:-30]


def get_activity() -> dict:
    with _ACT_LOCK:
        return {"tcp_events": list(reversed(_TCP_EVENTS)),
                "udp_counts": dict(_UDP_COUNTS)}


# ---- 負荷試験の共有状態 (UDP ループと制御ハンドラの間で共有) ----------------
_REG_LOCK = threading.Lock()
_LOAD_STATS: dict = {}    # src_ip -> {count, bytes, jitter, prev_transit}
_PUNCH: dict = {}         # nonce -> (addr, port)
_UDP_SOCK: socket.socket | None = None


def _note_load_packet(src_ip: str, data: bytes) -> None:
    try:
        _seq, t_send = decode_load(data)
    except (ValueError, UnicodeDecodeError):
        return
    transit_ms = (time.time_ns() - t_send) / 1e6  # クロック差込み(ジッタ計算で相殺)
    with _REG_LOCK:
        st = _LOAD_STATS.setdefault(
            src_ip, {"count": 0, "bytes": 0, "jitter": 0.0, "prev_transit": None})
        st["count"] += 1
        st["bytes"] += len(data)
        if st["prev_transit"] is not None:
            d = abs(transit_ms - st["prev_transit"])
            st["jitter"] += (d - st["jitter"]) / 16.0
        st["prev_transit"] = transit_ms


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer = self.client_address
        if not _src_allowed(peer[0]):
            log.warning("許可外の送信元からの TCP 接続を拒否: %s", peer[0])
            _note_tcp(peer[0], "DENIED")
            return
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                cmd = line.decode("ascii", "replace").strip()
                _note_tcp(peer[0], cmd.split()[0] if cmd else "?")
                if cmd == HELLO.decode().strip():
                    self.wfile.write(OK)
                    self.wfile.flush()
                elif cmd.startswith("TP_DOWN"):
                    self._send_bulk(float(cmd.split()[1]))
                    break   # 1 コネクション 1 計測 (ストリーム同期のズレ防止)
                elif cmd.startswith("TP_UP"):
                    self._recv_bulk()
                    break
                elif cmd.startswith("LD_UP"):
                    self._load_reset()      # 続けて LD_STAT が来るので break しない
                elif cmd.startswith("LD_STAT"):
                    self._load_stat()
                    break
                elif cmd.startswith("LD_DOWN"):
                    self._load_down(cmd)
                    break
                else:
                    break
        except (ConnectionError, ValueError, IndexError) as e:
            log.debug("control from %s ended: %s", peer, e)

    def _send_bulk(self, seconds: float) -> None:
        """seconds 秒間、クライアントへ連続送信 (ダウンリンク計測)."""
        deadline = time.monotonic() + seconds
        sock = self.connection
        try:
            while time.monotonic() < deadline:
                sock.sendall(_PAYLOAD)
        except (ConnectionError, OSError):
            pass

    def _recv_bulk(self) -> None:
        """クライアントからの EOF まで読み、受信バイト数を返す (アップリンク計測)."""
        total = 0
        sock = self.connection
        sock.settimeout(30)
        try:
            while True:
                data = sock.recv(CHUNK)
                if not data:
                    break
                if data.endswith(b"__END__"):
                    total += len(data) - len(b"__END__")
                    break
                total += len(data)
        except (ConnectionError, OSError, socket.timeout):
            pass
        try:
            self.wfile.write(f"BYTES {total}\n".encode())
            self.wfile.flush()
        except (ConnectionError, OSError):
            pass

    # ---- 負荷試験 (レートスイープ) ----------------------------------------
    def _load_reset(self) -> None:
        """上り負荷試験の開始: この IP からの負荷パケット統計をリセット."""
        ip = self.client_address[0]
        with _REG_LOCK:
            _LOAD_STATS[ip] = {"count": 0, "bytes": 0, "jitter": 0.0,
                               "prev_transit": None}
        self.wfile.write(b"READY\n")
        self.wfile.flush()

    def _load_stat(self) -> None:
        time.sleep(0.3)   # 飛行中パケットの到着を待つ
        ip = self.client_address[0]
        with _REG_LOCK:
            st = dict(_LOAD_STATS.get(ip) or
                      {"count": 0, "bytes": 0, "jitter": 0.0})
        self.wfile.write(
            f"LDSTAT {st['count']} {st['bytes']} {st['jitter']:.3f}\n".encode())
        self.wfile.flush()

    def _load_down(self, cmd: str) -> None:
        """下り負荷試験: punch 済みアドレスへ指定レートで送出し件数を返す."""
        try:
            _c, nonce, seconds_s, rate_s, payload_s = cmd.split()
            seconds, rate, payload = float(seconds_s), float(rate_s), int(payload_s)
        except ValueError:
            self.wfile.write(b"ERR bad-args\n")
            self.wfile.flush()
            return
        addr = None
        deadline = time.monotonic() + 2.0   # punch パケットの到着待ち
        while time.monotonic() < deadline:
            with _REG_LOCK:
                addr = _PUNCH.pop(nonce, None)
            if addr:
                break
            time.sleep(0.05)
        if addr is None or _UDP_SOCK is None:
            self.wfile.write(b"ERR no-punch\n")
            self.wfile.flush()
            return
        sent, nbytes = pace_send(lambda d: _UDP_SOCK.sendto(d, addr),
                                 seconds, rate, payload)
        self.wfile.write(f"LDSENT {sent} {nbytes}\n".encode())
        self.wfile.flush()


class _ThreadingTCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _udp_echo_loop(port: int, stop: threading.Event,
                   listen_ip: str = "0.0.0.0") -> None:
    global _UDP_SOCK
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((listen_ip, port))
    sock.settimeout(0.5)
    _UDP_SOCK = sock
    log.info("UDP echo listening on %s:%d", listen_ip, port)
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        if not _src_allowed(addr[0]):           # 許可外送信元: 無応答で破棄
            with _ACT_LOCK:
                _UDP_COUNTS["denied"] += 1
            continue
        if data.startswith(LOAD_PREFIX):        # 負荷パケット: 集計のみ (echo しない)
            with _ACT_LOCK:
                _UDP_COUNTS["load"] += 1
            _note_load_packet(addr[0], data)
        elif data.startswith(PUNCH_PREFIX):     # パンチ: 送信元を登録
            nonce = data[len(PUNCH_PREFIX):].split(b"\x00", 1)[0].decode(
                "ascii", "replace")
            with _REG_LOCK:
                _PUNCH[nonce] = addr
        else:                                    # 通常プローブ: そのまま echo
            with _ACT_LOCK:
                _UDP_COUNTS["echo"] += 1
            try:
                sock.sendto(data, addr)
            except OSError:
                pass
    _UDP_SOCK = None
    sock.close()


class NetqualServer:
    """受信サーバをバックグラウンドで起動/停止できるようにしたもの.

    統合エージェント(1 ソフトで送受兼用)から、ダッシュボードや測定ループと
    並行して常駐させるために使う。
    """

    def __init__(self, control_port: int, udp_port: int,
                 allowed_sources=None, iperf_port: int = 0,
                 iperf_bin: str = "iperf3", listen_ip: str = "0.0.0.0"):
        self.control_port = control_port
        self.udp_port = udp_port
        self.iperf_port = int(iperf_port or 0)   # 0 なら iperf3 サーバを起動しない
        self.iperf_bin = iperf_bin
        # 待受を特定 NIC の IP に限定 (本社: 固定IP のみで応答し Wi-Fi では応答しない)
        self.listen_ip = listen_ip or "0.0.0.0"
        set_allowed_sources(allowed_sources)
        self._stop = threading.Event()
        self._tcp: _ThreadingTCP | None = None
        self._threads: list[threading.Thread] = []
        self._iperf_proc = None

    def start(self) -> None:
        self._stop.clear()
        ut = threading.Thread(target=_udp_echo_loop,
                              args=(self.udp_port, self._stop, self.listen_ip),
                              daemon=True)
        ut.start()
        self._threads.append(ut)

        self._tcp = _ThreadingTCP((self.listen_ip, self.control_port),
                                  _ControlHandler)
        st = threading.Thread(target=self._tcp.serve_forever, daemon=True)
        st.start()
        self._threads.append(st)
        log.info("netqual server listening: %s TCP:%d / UDP:%d",
                 self.listen_ip, self.control_port, self.udp_port)
        self._start_iperf()

    def _start_iperf(self) -> None:
        if not self.iperf_port:
            return
        if shutil.which(self.iperf_bin) is None:
            log.warning("iperf_port 指定だが %s が見つからないため iperf3 サーバ未起動",
                        self.iperf_bin)
            return
        cmd = [self.iperf_bin, "-s", "-p", str(self.iperf_port)]
        if self.listen_ip and self.listen_ip != "0.0.0.0":
            cmd += ["-B", self.listen_ip]        # iperf3 も指定 NIC のみで待受
        try:
            self._iperf_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("iperf3 サーバ起動: %s:%d", self.listen_ip, self.iperf_port)
        except OSError as e:
            log.warning("iperf3 サーバ起動失敗: %s", e)

    def stop(self) -> None:
        self._stop.set()
        if self._tcp is not None:
            self._tcp.shutdown()
        if self._iperf_proc is not None:
            self._iperf_proc.terminate()
            self._iperf_proc = None


def run_server(control_port: int, udp_port: int,
               allowed_sources=None, iperf_port: int = 0,
               listen_ip: str = "0.0.0.0") -> None:
    """フォアグラウンドで受信サーバを起動 (`receiver` サブコマンド用)."""
    srv = NetqualServer(control_port, udp_port, allowed_sources,
                        iperf_port=iperf_port, listen_ip=listen_ip)
    srv.start()
    try:
        while not srv._stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="BDP Analyzer 受信側エージェント")
    ap.add_argument("--control-port", type=int, default=5301)
    ap.add_argument("--udp-port", type=int, default=5302)
    ap.add_argument("--allow", action="append", default=[],
                    help="許可する送信元 CIDR (複数指定可、未指定なら全許可)。"
                         "例: --allow 203.0.113.10/32 --allow 198.51.100.0/24")
    ap.add_argument("--iperf-port", type=int, default=0,
                    help="指定すると iperf3 サーバも起動 (例: 5201)。engine=iperf3 用")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="待受を特定 NIC の IP に限定 (例: 有線NIC IP)。"
                         "本社: 固定IPのみで応答し Wi-Fi では応答しない用途")
    args = ap.parse_args()
    run_server(args.control_port, args.udp_port, args.allow, args.iperf_port,
               listen_ip=args.bind)


if __name__ == "__main__":
    main()
