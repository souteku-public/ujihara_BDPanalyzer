"""送受信エージェント間のワイヤプロトコル定義.

外部ツール(iperf 等)に依存せず自己完結で測るための最小プロトコル。
送信側(sender/client)が測定を主導し、受信側(receiver/server)は応答する。

制御チャネル (TCP, control_port):
  - "HELLO\n"                         : 接続確認 → server は "OK\n"
  - "TP_DOWN <seconds>\n"             : server→client へバルク送信 (ダウンリンク計測)
                                        client は指定秒読み続けバイト数を計測
  - "TP_UP <seconds>\n"               : client→server へバルク送信 (アップリンク計測)
                                        server は読み捨て、終了後 "BYTES <n>\n" を返す
データチャネル (UDP, udp_port):
  - client → server にシーケンス付きプローブを送出、server はそのまま echo。
    往復した時刻差から RTT、到着間隔のばらつきから jitter、欠損からロスを算出。

UDP プローブのペイロード(先頭):
    "<seq>,<t_send_ns>"  (ASCII, 残りはパディング)
"""
from __future__ import annotations

# TCP スループット測定で使うチャンクサイズ
CHUNK = 64 * 1024
# UDP プローブの最小サイズ (パディング込み)
UDP_PAYLOAD = 1200

HELLO = b"HELLO\n"
OK = b"OK\n"


def encode_probe(seq: int, t_send_ns: int) -> bytes:
    head = f"{seq},{t_send_ns}".encode("ascii")
    if len(head) >= UDP_PAYLOAD:
        return head
    return head + b"\x00" * (UDP_PAYLOAD - len(head))


def decode_probe(data: bytes) -> tuple[int, int]:
    head = data.split(b"\x00", 1)[0].decode("ascii", "replace")
    seq_s, t_s = head.split(",", 1)
    return int(seq_s), int(t_s)
