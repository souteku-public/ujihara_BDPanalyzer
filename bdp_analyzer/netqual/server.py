"""受信側(receiver)エージェント.

対向 PC で常駐し、送信側(sender)からの測定要求に応答する。
役割:
  - TCP 制御サーバ: HELLO / TP_DOWN(バルク送出) / TP_UP(バルク受信) を処理
  - UDP エコーサーバ: 受け取ったプローブをそのまま返す (RTT/jitter/loss 用)

インターネット越しに使う想定なので、control_port(TCP) と udp_port(UDP) を
NAT/ファイアウォールで受信側に転送しておくこと。
"""
from __future__ import annotations

import logging
import os
import socket
import socketserver
import threading
import time

from .protocol import CHUNK, HELLO, OK

log = logging.getLogger("bdp.netqual.server")

_PAYLOAD = os.urandom(CHUNK)  # 送出用ダミーデータ


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer = self.client_address
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                cmd = line.decode("ascii", "replace").strip()
                if cmd == HELLO.decode().strip():
                    self.wfile.write(OK)
                    self.wfile.flush()
                elif cmd.startswith("TP_DOWN"):
                    self._send_bulk(float(cmd.split()[1]))
                    break   # 1 コネクション 1 計測 (ストリーム同期のズレ防止)
                elif cmd.startswith("TP_UP"):
                    self._recv_bulk()
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


class _ThreadingTCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _udp_echo_loop(port: int, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    log.info("UDP echo listening on :%d", port)
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            sock.sendto(data, addr)  # そのまま返す
        except OSError:
            pass
    sock.close()


def run_server(control_port: int, udp_port: int) -> None:
    stop = threading.Event()
    udp_thread = threading.Thread(target=_udp_echo_loop, args=(udp_port, stop),
                                  daemon=True)
    udp_thread.start()

    tcp = _ThreadingTCP(("0.0.0.0", control_port), _ControlHandler)
    log.info("TCP control listening on :%d", control_port)
    try:
        tcp.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        tcp.shutdown()


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="BDP Analyzer 受信側エージェント")
    ap.add_argument("--control-port", type=int, default=5301)
    ap.add_argument("--udp-port", type=int, default=5302)
    args = ap.parse_args()
    run_server(args.control_port, args.udp_port)


if __name__ == "__main__":
    main()
