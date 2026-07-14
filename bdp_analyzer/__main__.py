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
        if not args.loop:
            break
        time.sleep(args.interval)


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
    s.add_argument("--loop", action="store_true")
    s.add_argument("--interval", type=float, default=30)
    s.set_defaults(func=cmd_sender)

    p = sub.add_parser("predict", help="ハンドオーバー予測 単発表示")
    p.add_argument("--config", default="config.yaml")
    p.set_defaults(func=cmd_predict)

    args = ap.parse_args()
    _setup_log(args.log)
    args.func(args)


if __name__ == "__main__":
    main()
