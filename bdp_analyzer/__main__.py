"""BDP Analyzer エントリポイント.

使い方:
  # 受信側 PC (対向サーバ) を常駐
  python -m bdp_analyzer receiver --control-port 5301 --udp-port 5302

  # 送信側 / メイン PC: 監視 + ダッシュボードを起動
  python -m bdp_analyzer monitor --config config.yaml

  # ハンドオーバー予測を単発表示 (動作確認)
  python -m bdp_analyzer predict --config config.yaml
"""
from __future__ import annotations

import argparse
import logging
import time


def _setup_log(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_monitor(args) -> None:
    from .config import load_config
    from .orchestrator import Orchestrator
    from .webapp import server as web

    cfg = load_config(args.config)
    orch = Orchestrator(cfg)
    orch.start()

    if cfg.webapp.get("enabled", True):
        host = cfg.webapp.get("host", "0.0.0.0")
        port = int(cfg.webapp.get("port", 8080))
        logging.getLogger("bdp").info("ダッシュボード: http://%s:%d/", host, port)
        try:
            web.run(orch.storage, host, port, orchestrator=orch)  # ブロッキング
        except KeyboardInterrupt:
            pass
        finally:
            orch.stop()
    else:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            orch.stop()


def cmd_receiver(args) -> None:
    from .netqual.server import run_server
    run_server(args.control_port, args.udp_port)


def cmd_sender(args) -> None:
    from .netqual.client import measure_once
    while True:
        samples = measure_once(args.host, args.control_port, args.udp_port,
                               throughput_seconds=args.seconds,
                               streams=args.streams, omit_seconds=args.omit,
                               engine=args.engine, iperf_port=args.iperf_port)
        for s in samples:
            tp = f"{s.throughput_bps/1e6:.2f}Mbps" if s.throughput_bps else "n/a"
            print(f"{time.strftime('%H:%M:%S')} [{s.direction}] {tp} "
                  f"(x{s.streams}) rtt={s.rtt_ms}ms jitter={s.jitter_ms}ms "
                  f"loss={s.loss_pct}% ooo={s.out_of_order_pct}%")
        if args.csv:
            _append_net_csv(args.csv, samples)         # ローカル CSV に追記 (stdlib)
        if args.post:
            _post_samples(args.post, samples, args.token)  # 受信側へ送信 (stdlib)
        if not args.loop:
            break
        time.sleep(args.interval)


# 送信 CLI はインストール不要 (標準ライブラリのみ) で動くよう、CSV/POST も stdlib 実装
_NET_CSV_COLS = ["ts", "time_iso", "session", "direction", "throughput_bps",
                 "streams", "rtt_ms", "rtt_min_ms", "rtt_max_ms", "jitter_ms",
                 "loss_pct", "out_of_order_pct", "owd_ms"]


def _append_net_csv(path, samples) -> None:
    import csv
    import os
    from datetime import datetime
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(_NET_CSV_COLS)
        for s in samples:
            row = s.as_row()
            row["time_iso"] = datetime.fromtimestamp(row["ts"]).astimezone().isoformat()
            w.writerow(["" if row.get(c) is None else row.get(c) for c in _NET_CSV_COLS])


def _post_samples(url, samples, token) -> None:
    import json
    import urllib.request
    body = json.dumps({"samples": [s.as_row() for s in samples],
                       "token": token or ""}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        print(f"  (POST 失敗: {e})")


def cmd_inettest(args) -> None:
    """単独端末 (受信側 PC 不要) で公開エンドポイントへ速度測定."""
    from .netqual import inetspeed
    ep = {}
    if args.down_url:
        ep["down_url"] = args.down_url
    if args.up_url:
        ep["up_url"] = args.up_url
    if args.rtt_host:
        ep["rtt_host"] = args.rtt_host
    while True:
        samples = inetspeed.measure_public(
            endpoints=ep or None, seconds=args.seconds, streams=args.streams,
            session=args.label, bind_ip=args.bind)
        for s in samples:
            tp = f"{s.throughput_bps/1e6:.1f}Mbps" if s.throughput_bps else "n/a"
            print(f"{time.strftime('%H:%M:%S')} [{s.direction}] {tp} "
                  f"(x{s.streams}) rtt={s.rtt_ms}ms")
        if args.csv:
            _append_net_csv(args.csv, samples)
        if args.post:
            _post_samples(args.post, samples, args.token)
        if not args.loop:
            break
        time.sleep(args.interval)


def cmd_probe(args) -> None:
    """実機 WebGUI を探索し、endpoints/field_map の推奨値を提示する."""
    from .collectors.probe import WebGuiProber, format_report

    base = args.url
    user, pw, verify = args.user, args.password, args.verify_tls
    if not base and args.config:
        # config.yaml の該当コレクタから接続情報を流用
        from .config import load_config
        cfg = load_config(args.config)
        cols = [c for c in cfg.collectors
                if c.get("kind") in ("kymeta", "intellian")
                and (args.name is None or c.get("name") == args.name)]
        if not cols:
            print("config に kymeta/intellian コレクタが見つかりません。"
                  "--url で直接指定してください。")
            return
        c = cols[0]
        base = c.get("base_url")
        user = user or c.get("username")
        pw = pw if pw is not None else c.get("password")
        verify = c.get("verify_tls", False) if not args.verify_tls else True
        print(f"config のコレクタ '{c.get('name')}' の接続情報を使用します。")
    if not base:
        print("--url または --config を指定してください。")
        return

    print(f"探索中… {base} (数十秒かかることがあります)\n")
    prober = WebGuiProber(base, username=user, password=pw, verify_tls=verify)
    print(format_report(prober.run()))


def cmd_predict(args) -> None:
    import os
    from .config import load_config
    from .handover.predict import HandoverPredictor

    cfg = load_config(args.config)
    cache_dir = os.path.join(os.path.dirname(cfg.db_path) or ".", "tle")
    p = HandoverPredictor(cfg.handover, cfg.ground_station, cache_dir)
    serving, events = p.predict(time.time())
    print("=== 現在のサービス衛星 (推定) ===")
    for con, info in serving.items():
        print(f"  {con}: {info}")
    print("=== ハンドオーバー予測 ===")
    for e in events[:20]:
        print(f"  +{e.lead_time_s:>5.0f}s {e.constellation} {e.from_sat} -> {e.to_sat} "
              f"({e.reason}, el {e.from_elevation_deg}->{e.to_elevation_deg})")
    if not events:
        print("  (予測イベントなし)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="bdp_analyzer", description="LEO 通信品質モニタ")
    ap.add_argument("--log", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("monitor", help="監視 + ダッシュボード (メイン PC)")
    m.add_argument("--config", default="config.yaml")
    m.set_defaults(func=cmd_monitor)

    r = sub.add_parser("receiver", help="ネット品質 受信側サーバ (対向 PC)")
    r.add_argument("--control-port", type=int, default=5301)
    r.add_argument("--udp-port", type=int, default=5302)
    r.set_defaults(func=cmd_receiver)

    s = sub.add_parser("sender", help="ネット品質 送信側 単発/ループ測定")
    s.add_argument("host")
    s.add_argument("--control-port", type=int, default=5301)
    s.add_argument("--udp-port", type=int, default=5302)
    s.add_argument("--seconds", type=float, default=5)
    s.add_argument("--streams", type=int, default=1,
                   help="並列 TCP ストリーム数 (iperf -P 相当)")
    s.add_argument("--omit", type=float, default=0.0,
                   help="スロースタート除外秒 (iperf --omit 相当)")
    s.add_argument("--engine", choices=["builtin", "iperf3"], default="builtin",
                   help="スループット測定エンジン (iperf3 は対向に iperf3 サーバ要)")
    s.add_argument("--iperf-port", type=int, default=5201)
    s.add_argument("--csv", help="結果をこの CSV に追記 (ローカル保存・インストール不要)")
    s.add_argument("--post", help="結果を受信側の /api/ingest へ送信し中央記録 "
                                  "(例: http://<受信IP>:8080/api/ingest)")
    s.add_argument("--token", default="", help="--post 時の ingest_token (設定時のみ)")
    s.add_argument("--loop", action="store_true")
    s.add_argument("--interval", type=float, default=30)
    s.set_defaults(func=cmd_sender)

    it = sub.add_parser("inettest",
                        help="単独端末で公開エンドポイントへ速度測定 (受信側 PC 不要)")
    it.add_argument("--seconds", type=float, default=10, help="上り/下り各測定の秒数")
    it.add_argument("--streams", type=int, default=4, help="並列接続数 (高BDP対策)")
    it.add_argument("--label", default="インターネット速度", help="測定先ラベル")
    it.add_argument("--bind", help="送信元IP (マルチNIC時)")
    it.add_argument("--down-url", help="下り用URL (既定: Cloudflare)")
    it.add_argument("--up-url", help="上り用URL (既定: Cloudflare)")
    it.add_argument("--rtt-host", help="RTT計測先ホスト (既定: speed.cloudflare.com)")
    it.add_argument("--csv", help="結果をこの CSV に追記")
    it.add_argument("--post", help="結果を受信側 /api/ingest へ送信")
    it.add_argument("--token", default="")
    it.add_argument("--loop", action="store_true")
    it.add_argument("--interval", type=float, default=60)
    it.set_defaults(func=cmd_inettest)

    pr = sub.add_parser("probe",
                        help="実機 WebGUI を探索し endpoints/field_map を提案")
    pr.add_argument("--config", default="config.yaml",
                    help="接続情報を流用する config (既定: config.yaml)")
    pr.add_argument("--name", help="config 内の対象コレクタ名 (複数ある場合)")
    pr.add_argument("--url", help="直接指定する WebGUI のベース URL "
                                  "(例: https://192.168.44.2)")
    pr.add_argument("--user", help="Basic 認証ユーザ (未指定なら config)")
    pr.add_argument("--password", help="Basic 認証パスワード (未指定なら config)")
    pr.add_argument("--verify-tls", action="store_true",
                    help="TLS 証明書を検証する (既定: 検証しない)")
    pr.set_defaults(func=cmd_probe)

    p = sub.add_parser("predict", help="ハンドオーバー予測 単発表示")
    p.add_argument("--config", default="config.yaml")
    p.set_defaults(func=cmd_predict)

    args = ap.parse_args()
    _setup_log(args.log)
    args.func(args)


if __name__ == "__main__":
    main()
