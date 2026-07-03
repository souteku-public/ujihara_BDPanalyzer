"""送受信エージェント間のワイヤプロトコル定義.

外部ツール(iperf 等)に依存せず自己完結で測るための最小プロトコル。
送信側(sender/client)が測定を主導し、受信側(receiver/server)は応答する。

制御チャネル (TCP, control_port):
  - "HELLO\n"                         : 接続確認 → server は "OK\n"
  - "TP_DOWN <seconds>\n"             : server→client へバルク送信 (ダウンリンク計測)
                                        client は指定秒読み続けバイト数を計測
  - "TP_UP <seconds>\n"               : client→server へバルク送信 (アップリンク計測)
                                        server は読み捨て、終了後 "BYTES <n>\n" を返す
  - "LD_UP <seconds>\n"               : 上り負荷試験の開始宣言。server は同一 IP からの
                                        負荷パケット統計をリセットし "READY\n" を返す
  - "LD_STAT\n"                       : 負荷試験の受信統計を要求 →
                                        "LDSTAT <count> <bytes> <jitter_ms>\n"
  - "LD_DOWN <nonce> <seconds> <rate_bps> <payload>\n"
                                      : 下り負荷試験。server は punch 済み UDP アドレスへ
                                        指定レートでペーシング送出し "LDSENT <count> <bytes>\n"
データチャネル (UDP, udp_port):
  - プローブ  "<seq>,<t_send_ns>"     : server がそのまま echo → RTT/jitter/loss
  - 負荷      "L,<seq>,<t_send_ns>"   : echo されない。server は受信数/バイト/ジッタを集計
  - パンチ    "P,<nonce>"             : 下り負荷試験用に送信元アドレスを登録 (NAT 穴あけ兼用)
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


# ---- 負荷試験 (レートスイープ) 用 -----------------------------------------
LOAD_PREFIX = b"L,"
PUNCH_PREFIX = b"P,"


def encode_load(seq: int, t_send_ns: int, size: int = UDP_PAYLOAD) -> bytes:
    head = f"L,{seq},{t_send_ns}".encode("ascii")
    if len(head) >= size:
        return head
    return head + b"\x00" * (size - len(head))


def decode_load(data: bytes) -> tuple[int, int]:
    head = data.split(b"\x00", 1)[0].decode("ascii", "replace")
    _l, seq_s, t_s = head.split(",", 2)
    return int(seq_s), int(t_s)


def encode_punch(nonce: str) -> bytes:
    return PUNCH_PREFIX + nonce.encode("ascii")


def pace_send(send_fn, seconds: float, rate_bps: float,
              payload: int = UDP_PAYLOAD) -> tuple[int, int]:
    """指定レートでペーシングしながら負荷パケットを送出する.

    send_fn(bytes) を呼ぶだけの汎用実装 (connected socket の send / sendto 両対応)。
    戻り値は (送出パケット数, 送出バイト数)。
    """
    import time as _t
    interval = (payload * 8) / max(rate_bps, 1.0)
    end = _t.monotonic() + seconds
    next_t = _t.monotonic()
    seq = sent = nbytes = 0
    while _t.monotonic() < end:
        now = _t.monotonic()
        if now < next_t:
            _t.sleep(min(0.005, next_t - now))
            continue
        data = encode_load(seq, _t.time_ns(), payload)
        try:
            send_fn(data)
            sent += 1
            nbytes += len(data)
        except OSError:
            pass
        seq += 1
        next_t += interval
        if next_t < now - 0.1:   # 送出が追いつかない場合は現在時刻に再同期
            next_t = now
    return sent, nbytes
