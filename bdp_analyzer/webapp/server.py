"""Flask ダッシュボード.

SINR と スループット/ジッタ/RTT の対比、アンテナの向き、衛星の可視状況と
ハンドオーバー予測タイムラインを表示する。API は JSON を返し、フロントは
Chart.js で描画する。
"""
from __future__ import annotations

import time
from typing import Optional

from flask import Flask, jsonify, render_template, request

from ..storage import Storage


def create_app(storage: Storage, orchestrator=None) -> Flask:
    app = Flask(__name__)

    def _since() -> float:
        minutes = float(request.args.get("minutes", 30))
        return time.time() - minutes * 60

    @app.route("/")
    def index():
        return render_template("dashboard.html")

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

    @app.route("/api/targets", methods=["POST"])
    def api_target_add():
        if orchestrator is None:
            return jsonify({"ok": False, "error": "orchestrator なし"}), 400
        body = request.get_json(force=True, silent=True) or {}
        job_id = orchestrator.add_net_target(
            body.get("label", ""), body.get("host", ""),
            control_port=body.get("control_port"),
            udp_port=body.get("udp_port"),
            interval_s=body.get("interval_s"))
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

    return app


def run(storage: Storage, host: str, port: int, orchestrator=None) -> None:
    app = create_app(storage, orchestrator)
    app.run(host=host, port=port, threaded=True, use_reloader=False)
