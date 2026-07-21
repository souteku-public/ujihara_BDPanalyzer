"""Flask ダッシュボード.

SINR と スループット/ジッタ/RTT の対比、アンテナの向き、衛星の可視状況と
ハンドオーバー予測タイムラインを表示する。API は JSON を返し、フロントは
Chart.js で描画する。CSV エクスポート (/export/*.csv) も提供する。
"""
from __future__ import annotations

import csv
import io
import math
import time
from datetime import datetime
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request

from ..storage import Storage

# CSV の列順 (データが空でもこの順でヘッダを出す。docs/data_format.md と一致)
_EXPORT_COLUMNS = {
    "rf": ["ts", "time_iso", "source", "kind", "sinr_db", "snr_above_noise",
           "rssi_dbm", "azimuth_deg", "elevation_deg", "tilt_deg",
           "tx_freq_mhz", "rx_freq_mhz", "beam_id", "satellite_id",
           "down_bps", "up_bps", "latency_ms", "drop_rate",
           "obstruction_pct", "state"],
    "net": ["ts", "time_iso", "session", "direction", "throughput_bps",
            "streams", "rtt_ms", "rtt_min_ms", "rtt_max_ms", "jitter_ms",
            "loss_pct", "out_of_order_pct", "owd_ms"],
    "load": ["ts", "time_iso", "session", "direction", "offered_bps",
             "achieved_bps", "loss_pct", "jitter_ms", "rtt_ms"],
    "handover": ["ts", "time_iso", "constellation", "from_sat", "to_sat",
                 "reason", "from_elevation_deg", "to_elevation_deg",
                 "lead_time_s"],
    "markers": ["ts", "time_iso", "text"],
    "weather": ["ts", "time_iso", "precip_mmh", "rain_mmh", "cloud_cover_pct",
                "temp_c", "humidity_pct", "weather_code", "wind_speed_ms",
                "rain_atten_ku45_db"],
}


def create_app(storage: Storage, orchestrator=None) -> Flask:
    app = Flask(__name__)

    def _since() -> float:
        minutes = float(request.args.get("minutes", 30))
        return time.time() - minutes * 60

    @app.route("/")
    def index():
        from .. import __version__
        return render_template("dashboard.html", version=__version__)

    @app.route("/viewer")
    def viewer():
        # CSV 可視化ビューア (単体でも動く自己完結ページ)
        return render_template("csv_viewer.html")

    @app.route("/api/sources")
    def api_sources():
        return jsonify(storage.sources())

    @app.route("/api/rf")
    def api_rf():
        source = request.args.get("source") or None
        return jsonify(storage.recent_rf(_since(), source))

    @app.route("/api/net")
    def api_net():
        return jsonify(storage.recent_net(_since()))

    @app.route("/api/correlation")
    def api_correlation():
        window = float(request.args.get("window", 30))
        return jsonify(storage.correlated(_since(), window))

    @app.route("/api/handovers")
    def api_handovers():
        # 過去〜近未来の予測イベント両方を返す
        rows = storage.recent_handovers(_since())
        now = time.time()
        for r in rows:
            r["future"] = r["ts"] > now
        return jsonify(rows)

    @app.route("/api/serving")
    def api_serving():
        return jsonify(getattr(orchestrator, "latest_serving", {}) if orchestrator else {})

    # ---- 計測コントロール (UI から実行時に ON/OFF・追加) --------------------
    @app.route("/api/jobs")
    def api_jobs():
        return jsonify(orchestrator.jobs_status() if orchestrator else [])

    @app.route("/api/jobs/<path:job_id>/enable", methods=["POST"])
    def api_job_enable(job_id: str):
        if orchestrator is None:
            return jsonify({"ok": False, "error": "orchestrator なし"}), 400
        body = request.get_json(force=True, silent=True) or {}
        ok = orchestrator.set_enabled(job_id, bool(body.get("enabled")))
        return jsonify({"ok": ok}), (200 if ok else 404)

    @app.route("/api/jobs/<path:job_id>/interval", methods=["POST"])
    def api_job_interval(job_id: str):
        if orchestrator is None:
            return jsonify({"ok": False, "error": "orchestrator なし"}), 400
        body = request.get_json(force=True, silent=True) or {}
        err = orchestrator.set_job_interval(job_id, body.get("interval_s"))
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True})

    @app.route("/api/settings")
    def api_settings():
        return jsonify(orchestrator.get_settings() if orchestrator else {})

    @app.route("/api/settings", methods=["POST"])
    def api_settings_save():
        if orchestrator is None:
            return jsonify({"ok": False}), 400
        body = request.get_json(force=True, silent=True) or {}
        return jsonify({"ok": True, "settings": orchestrator.set_settings(body)})

    @app.route("/api/targets", methods=["POST"])
    def api_target_add():
        if orchestrator is None:
            return jsonify({"ok": False, "error": "orchestrator なし"}), 400
        body = request.get_json(force=True, silent=True) or {}
        mode = (body.get("mode") or "").strip() or None
        job_id = orchestrator.add_net_target(
            body.get("label", ""), body.get("host", ""),
            mode=mode,
            control_port=body.get("control_port"),
            udp_port=body.get("udp_port"),
            interval_s=body.get("interval_s"),
            throughput_seconds=body.get("throughput_seconds"),
            streams=body.get("streams"),
            bind_ip=(body.get("bind_ip") or "").strip() or None)
        if job_id is None:
            return jsonify({"ok": False,
                            "error": "ホスト未指定か同名ラベルが既にあります"}), 400
        return jsonify({"ok": True, "id": job_id})

    @app.route("/api/targets/<path:job_id>", methods=["DELETE"])
    def api_target_delete(job_id: str):
        if orchestrator is None:
            return jsonify({"ok": False, "error": "orchestrator なし"}), 400
        ok = orchestrator.remove_net_target(job_id)
        return jsonify({"ok": ok}), (200 if ok else 404)

    # ---- ネットワーク情報 --------------------------------------------------
    @app.route("/api/netinfo")
    def api_netinfo():
        return jsonify(orchestrator.get_netinfo() if orchestrator else {})

    @app.route("/api/netinfo", methods=["POST"])
    def api_netinfo_save():
        if orchestrator is None:
            return jsonify({"ok": False}), 400
        body = request.get_json(force=True, silent=True) or {}
        return jsonify({"ok": True,
                        "profile": orchestrator.set_net_profile(body)})

    @app.route("/api/netinfo/global", methods=["POST"])
    def api_netinfo_global():
        if orchestrator is None:
            return jsonify({"ok": False}), 400
        ip = orchestrator.refresh_global_ip()
        return jsonify({"ok": ip is not None, "global_ip": ip})

    # ---- 負荷耐性テスト ------------------------------------------------------
    @app.route("/api/loadtest", methods=["POST"])
    def api_loadtest_start():
        if orchestrator is None:
            return jsonify({"ok": False, "error": "orchestrator なし"}), 400
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("type", "sweep")
        try:
            if mode == "soak":
                # 耐久試験: 一定レートをチャンク刻みで流し続ける
                rate = float(body.get("rate_mbps"))
                duration = float(body.get("duration_s"))
                chunk = max(1.0, min(float(body.get("chunk_s", 10)), duration))
                n = max(1, int(math.ceil(duration / chunk)))
                rates = [rate * 1e6] * n
                step_s = chunk
            else:
                rates = [float(m) * 1e6 for m in body.get("rates_mbps", [])]
                step_s = float(body.get("step_s", 5))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "レート/秒数が不正です"}), 400
        err = orchestrator.start_load_test(
            body.get("target", ""), body.get("direction", "uplink"),
            rates, step_s, mode=mode)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True})

    @app.route("/api/serverstats")
    def api_serverstats():
        from ..netqual import server as nsv
        running = orchestrator is not None and \
            orchestrator.netqual_server is not None
        d = orchestrator._net_defaults() if orchestrator else {}
        srv = orchestrator.netqual_server if orchestrator else None
        iperf_running = bool(srv and getattr(srv, "_iperf_proc", None) is not None)
        return jsonify({
            "running": running,
            "control_port": d.get("control_port"),
            "udp_port": d.get("udp_port"),
            "engine": d.get("engine"),
            "iperf_running": iperf_running,
            "iperf_port": d.get("iperf_port") if iperf_running else None,
            "allowed": [str(n) for n in nsv._ALLOWED_NETS] or ["(全許可)"],
            **nsv.get_activity(),
        })

    @app.route("/api/loadtest")
    def api_loadtest_status():
        if orchestrator is None:
            return jsonify({"running": False, "results": []})
        return jsonify(orchestrator.get_load_status())

    @app.route("/api/loadtests")
    def api_loadtest_history():
        return jsonify(storage.recent_load_tests(_since()))

    @app.route("/api/weather")
    def api_weather():
        return jsonify(storage.recent_weather(_since()))

    @app.route("/api/ingest", methods=["POST"])
    def api_ingest():
        # インストール制限のある送信 PC (sender CLI) から測定結果を受け取り中央記録する。
        # 送信側は stdlib のみ (urllib) で POST でき、受信側でダッシュボード/DB に反映される。
        import dataclasses
        from ..model import NetSample
        body = request.get_json(force=True, silent=True) or {}
        token_cfg = (orchestrator.config.netqual.get("ingest_token")
                     if orchestrator else None)
        if token_cfg and body.get("token") != token_cfg:
            return jsonify({"ok": False, "error": "token 不一致"}), 403
        fields = {f.name for f in dataclasses.fields(NetSample)}
        stored = 0
        for s in body.get("samples", []):
            row = {k: v for k, v in s.items() if k in fields}
            if "ts" not in row or "session" not in row:
                continue
            try:
                storage.add_net(NetSample(**row))
                stored += 1
            except (TypeError, ValueError):
                continue
        return jsonify({"ok": True, "stored": stored})

    # ---- 実験マーカー (アンテナ間距離の変更等をデータに刻む) ------------------
    @app.route("/api/markers")
    def api_markers():
        return jsonify(storage.recent_markers(_since()))

    @app.route("/api/markers", methods=["POST"])
    def api_marker_add():
        body = request.get_json(force=True, silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "テキストが空です"}), 400
        storage.add_marker(time.time(), text)
        return jsonify({"ok": True})

    # ---- CSV エクスポート ----------------------------------------------------
    @app.route("/export/<kind>.csv")
    def export_csv(kind: str):
        fetchers = {
            "rf": storage.recent_rf,
            "net": storage.recent_net,
            "load": storage.recent_load_tests,
            "handover": storage.recent_handovers,
            "markers": storage.recent_markers,
            "weather": storage.recent_weather,
        }
        if kind not in fetchers:
            return jsonify({"error": "rf / net / load / handover"}), 404
        cols = _EXPORT_COLUMNS[kind]
        rows = fetchers[kind](_since())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            r = dict(r)
            # Excel 等で扱いやすいよう ISO 形式の時刻列を付与 (ローカル時刻)
            r["time_iso"] = datetime.fromtimestamp(r["ts"]).astimezone().isoformat()
            w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in cols])
        fname = f"bdp_{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            buf.getvalue().encode("utf-8-sig"),   # BOM 付き (Excel の文字化け対策)
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fname}"})

    return app


def run(storage: Storage, host: str, port: int, orchestrator=None) -> None:
    app = create_app(storage, orchestrator)
    app.run(host=host, port=port, threaded=True, use_reloader=False)
