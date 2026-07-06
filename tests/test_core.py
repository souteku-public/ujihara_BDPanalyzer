"""コア機能のテスト (HW / ネット不要で動く範囲)."""
import os
import tempfile
import threading
import time

import pytest

from bdp_analyzer.model import RFSample, NetSample
from bdp_analyzer.storage import Storage
from bdp_analyzer.collectors.base import dig, to_float
from bdp_analyzer.netqual.protocol import encode_probe, decode_probe


def test_dig_and_to_float():
    obj = {"a": {"b": [{"c": "3.5"}]}}
    assert dig(obj, "a.b.0.c") == "3.5"
    assert dig(obj, "a.x") is None
    assert to_float("3.5") == 3.5
    assert to_float(None) is None
    assert to_float("abc") is None


def test_probe_roundtrip():
    payload = encode_probe(42, 123456789)
    seq, t = decode_probe(payload)
    assert seq == 42 and t == 123456789
    assert len(payload) >= 1200


def test_storage_and_correlation():
    with tempfile.TemporaryDirectory() as d:
        st = Storage(os.path.join(d, "t.sqlite"))
        now = time.time()
        st.add_rf(RFSample(ts=now, source="s1", kind="starlink", sinr_db=8.0,
                           azimuth_deg=120, elevation_deg=45))
        st.add_net(NetSample(ts=now + 1, session="peer", direction="downlink",
                            throughput_bps=1e8, rtt_ms=40, jitter_ms=2, loss_pct=0.1))
        assert st.sources() == ["s1"]
        corr = st.correlated(now - 60, window_s=30)
        assert len(corr) == 1
        assert corr[0]["sinr_db"] == 8.0          # RF が突き合わされている
        assert corr[0]["throughput_bps"] == 1e8


def test_bool_stored_as_int():
    with tempfile.TemporaryDirectory() as d:
        st = Storage(os.path.join(d, "t.sqlite"))
        st.add_rf(RFSample(ts=time.time(), source="s", kind="starlink",
                           snr_above_noise=True))
        rows = st.recent_rf(0)
        assert rows[0]["snr_above_noise"] == 1


def test_netqual_loopback():
    """server を localhost で起動し、client で 1 サイクル測定できること."""
    from bdp_analyzer.netqual.server import run_server
    from bdp_analyzer.netqual import client

    cport, uport = 15301, 15302
    t = threading.Thread(target=run_server, args=(cport, uport), daemon=True)
    t.start()
    time.sleep(0.5)

    samples = client.measure_once("127.0.0.1", cport, uport,
                                  throughput_seconds=1, udp_probe_count=20,
                                  udp_probe_interval_ms=5)
    assert len(samples) == 2
    down = samples[0]
    assert down.throughput_bps and down.throughput_bps > 0
    assert down.loss_pct is not None
    assert down.rtt_ms is not None      # ループバックなので必ず戻る


def test_ui_job_control_and_targets():
    """UI からのジョブ ON/OFF・測定先追加/削除・状態の再起動復元."""
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator
    from bdp_analyzer.webapp.server import create_app

    with tempfile.TemporaryDirectory() as d:
        def mkcfg():
            return Config(
                raw={}, ground_station=GroundStation(),
                db_path=os.path.join(d, "t.sqlite"), collectors=[],
                netqual={"enabled": True, "role": "both",
                         "control_port": 15531, "udp_port": 15532,
                         "interval_s": 2, "throughput_seconds": 0.3,
                         "udp_probe_count": 5, "udp_probe_interval_ms": 5,
                         "targets": []},
                handover={"enabled": False}, webapp={})

        orch = Orchestrator(mkcfg())
        orch.start()
        try:
            c = create_app(orch.storage, orch).test_client()
            jobs = {j["id"]: j for j in c.get("/api/jobs").get_json()}
            assert jobs["netqual-server"]["enabled"]

            # 測定先追加 → ラベル付きで記録される
            r = c.post("/api/targets", json={"label": "LANチェック",
                                             "host": "127.0.0.1"})
            assert r.get_json()["ok"]
            time.sleep(3)
            assert "LANチェック" in {row["session"]
                                     for row in orch.storage.recent_net(0)}

            # OFF → (仕掛かり分の完了後) 測定停止
            jid = "net:LANチェック"
            assert c.post(f"/api/jobs/{jid}/enable",
                          json={"enabled": False}).get_json()["ok"]
            time.sleep(2)
            n0 = len(orch.storage.recent_net(0))
            time.sleep(2.5)
            assert len(orch.storage.recent_net(0)) == n0
        finally:
            orch.stop()

        # 再起動 → 追加した測定先と OFF 状態が復元される
        orch2 = Orchestrator(mkcfg())
        orch2.start()
        try:
            j2 = {j["id"]: j for j in orch2.jobs_status()}
            assert "net:LANチェック" in j2
            assert j2["net:LANチェック"]["enabled"] is False
            assert orch2.remove_net_target("net:LANチェック")
        finally:
            orch2.stop()


def test_loadtest_sweep_loopback():
    """負荷耐性テスト: ループバックで上り/下りスイープが完走し DB に残る."""
    from bdp_analyzer.netqual.server import NetqualServer
    from bdp_analyzer.netqual import loadtest

    srv = NetqualServer(15551, 15552)
    srv.start()
    try:
        time.sleep(0.3)
        for direction in ("uplink", "downlink"):
            results = loadtest.run_sweep(
                "127.0.0.1", 15551, 15552, direction=direction,
                rates_bps=[2e6], step_seconds=1, session="lo")
            assert len(results) == 1
            r = results[0]
            assert r.get("error") is None
            assert r["achieved_bps"] > 2e6 * 0.7
            assert r["loss_pct"] is not None and r["loss_pct"] < 20
            assert r["rtt_ms"] is not None      # 負荷時 RTT
    finally:
        srv.stop()


def test_netinfo_profile_roundtrip():
    """ネットワーク情報: 自動検出とプロファイル保存/復元."""
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as d:
        def mkcfg():
            return Config(raw={}, ground_station=GroundStation(),
                          db_path=os.path.join(d, "t.sqlite"), collectors=[],
                          netqual={"enabled": False}, handover={"enabled": False},
                          webapp={})
        orch = Orchestrator(mkcfg())
        orch.start()
        try:
            info = orch.get_netinfo()
            assert info["hostname"]
            orch.set_net_profile({"line_type": "static_global",
                                  "peer_global_ip": "198.51.100.5"})
        finally:
            orch.stop()

        orch2 = Orchestrator(mkcfg())
        orch2.start()
        try:
            p = orch2.get_netinfo()["profile"]
            assert p["line_type"] == "static_global"
            assert p["peer_global_ip"] == "198.51.100.5"
        finally:
            orch2.stop()


def test_csv_export():
    """CSV エクスポート: 全種別で固定ヘッダが出て、データ行が入る."""
    from bdp_analyzer.webapp.server import create_app

    with tempfile.TemporaryDirectory() as d:
        st = Storage(os.path.join(d, "t.sqlite"))
        now = time.time()
        st.add_rf(RFSample(ts=now, source="s1", kind="starlink", sinr_db=7.5))
        st.add_net(NetSample(ts=now, session="Wi-Fi", direction="downlink",
                             throughput_bps=5e7, rtt_ms=12.3))
        c = create_app(st, None).test_client()

        for kind in ("rf", "net", "load", "handover"):
            resp = c.get(f"/export/{kind}.csv?minutes=60")
            assert resp.status_code == 200
            body = resp.data.decode("utf-8-sig")
            assert body.splitlines()[0].startswith("ts,time_iso,")

        net_csv = c.get("/export/net.csv?minutes=60").data.decode("utf-8-sig")
        assert "Wi-Fi" in net_csv and "downlink" in net_csv
        rf_csv = c.get("/export/rf.csv?minutes=60").data.decode("utf-8-sig")
        assert "starlink" in rf_csv and "7.5" in rf_csv
        assert c.get("/export/bad.csv").status_code == 404


def test_starlink_history_diff_collection():
    """Starlink get_history: 1 秒刻み履歴の差分回収 (grpcurl をモック)."""
    from bdp_analyzer.collectors.starlink import StarlinkCollector

    col = StarlinkCollector({"name": "sl", "interval_s": 5, "history": True})
    ring = 900

    def make(current):
        return {"dishGetHistory": {
            "current": current,
            "popPingLatencyMs": [30.0] * ring,
            "downlinkThroughputBps": [1e6] * ring,
            "uplinkThroughputBps": [1e5] * ring,
            "popPingDropRate": [0.0] * ring, "snr": [0] * ring}}

    state = {"n": 0}

    def fake(payload='{"get_status":{}}'):
        if "get_status" in payload:
            return {"dishGetStatus": {"state": "CONNECTED"}}
        state["n"] += 1
        return make(1000 if state["n"] == 1 else 1007)

    col._run_grpcurl = fake
    now = time.time()
    h1 = [s for s in col.poll_many(now) if s.source == "sl:1s"]
    assert len(h1) == 7                       # 初回 = interval_s + 2
    h2 = [s for s in col.poll_many(now + 7) if s.source == "sl:1s"]
    assert len(h2) == 7                       # current の増分 (1000→1007)
    ts = [s.ts for s in h2]
    assert all(abs((ts[i + 1] - ts[i]) - 1.0) < 0.01 for i in range(len(ts) - 1))
    assert h2[-1].latency_ms == 30.0 and h2[-1].down_bps == 1e6


def test_rtt_monitor_finalizes_while_running():
    """常時 RTT モニタ: 停止を待たず稼働中に 1 秒窓が確定する (回帰テスト)."""
    import threading
    from bdp_analyzer.netqual.server import NetqualServer
    from bdp_analyzer.netqual import rttmon

    srv = NetqualServer(15721, 15722)
    srv.start()
    samples = []
    stop = threading.Event()
    t = threading.Thread(target=rttmon.run_monitor, daemon=True, kwargs=dict(
        host="127.0.0.1", udp_port=15722, stop=stop, on_sample=samples.append,
        session="lo", probe_interval_ms=100, agg_seconds=1))
    try:
        time.sleep(0.3)
        t.start()
        time.sleep(3.5)
        n_running = len(samples)              # ← 停止前に確定していること
        assert n_running >= 2, "稼働中に窓が確定していない"
    finally:
        stop.set()
        t.join(timeout=2)
        srv.stop()
    assert all(s.direction == "rtt" for s in samples)
    ok = [s for s in samples if s.rtt_ms is not None]
    assert ok and all(s.loss_pct is not None and s.loss_pct < 50 for s in ok)


def test_interval_and_settings_api():
    """実行間隔の変更 (クランプ・永続化) と測定設定 API."""
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as d:
        def mkcfg():
            return Config(raw={}, ground_station=GroundStation(),
                          db_path=os.path.join(d, "t.sqlite"), collectors=[],
                          netqual={"enabled": True, "role": "both",
                                   "control_port": 15731, "udp_port": 15732,
                                   "interval_s": 600,
                                   "targets": [{"label": "lo",
                                                "host": "127.0.0.1",
                                                "enabled": False}]},
                          handover={"enabled": False}, webapp={})
        orch = Orchestrator(mkcfg())
        orch.start()
        try:
            assert orch.set_job_interval("net:lo", 1) is None   # 下限 5 にクランプ
            assert orch.jobs["net:lo"]["interval_s"] == 5
            assert orch.set_job_interval("rttmon:lo", 10) is not None
            orch.set_settings({"throughput_seconds": 999,
                               "rtt_probe_interval_ms": 10})
            s = orch.get_settings()
            assert s["throughput_seconds"]["value"] == 30       # max クランプ
            assert s["rtt_probe_interval_ms"]["value"] == 50    # min クランプ
            assert s["rtt_agg_seconds"]["default"] == 1         # 既定値メモ
        finally:
            orch.stop()

        orch2 = Orchestrator(mkcfg())
        orch2.start()
        try:
            assert orch2.jobs["net:lo"]["interval_s"] == 5      # 再起動後も維持
            assert orch2.get_settings()["throughput_seconds"]["value"] == 30
        finally:
            orch2.stop()


def test_bind_ip_multi_nic():
    """マルチ NIC 用の送信元固定 (bind_ip) が全測定経路で機能する."""
    import threading
    from bdp_analyzer.netqual.server import NetqualServer
    from bdp_analyzer.netqual import client, loadtest, rttmon

    srv = NetqualServer(15771, 15772)
    srv.start()
    try:
        time.sleep(0.3)
        samples = client.measure_once(
            "127.0.0.1", 15771, 15772, throughput_seconds=0.3,
            udp_probe_count=5, udp_probe_interval_ms=5, bind_ip="127.0.0.1")
        assert samples[0].throughput_bps > 0
        assert samples[0].rtt_ms is not None

        r = loadtest.run_sweep(
            "127.0.0.1", 15771, 15772, direction="downlink",
            rates_bps=[2e6], step_seconds=1, session="lo",
            bind_ip="127.0.0.1")[0]
        assert r.get("error") is None and r["achieved_bps"] > 1e6

        out = []
        stop = threading.Event()
        t = threading.Thread(target=rttmon.run_monitor, daemon=True, kwargs=dict(
            host="127.0.0.1", udp_port=15772, stop=stop, on_sample=out.append,
            session="lo", probe_interval_ms=100, agg_seconds=1,
            bind_ip="127.0.0.1"))
        t.start()
        time.sleep(2.5)
        assert len(out) >= 1 and out[0].rtt_ms is not None
        stop.set()
        t.join(timeout=2)
    finally:
        srv.stop()


def test_sinr_model_and_markers():
    """SINR 理論値 (リンクバジェット) の記録と実験マーカー."""
    from bdp_analyzer.handover.linkbudget import estimate_sinr_db
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator
    from bdp_analyzer.webapp.server import create_app

    # 代表値で桁が合い、距離・仰角の悪化で単調に下がる
    near = estimate_sinr_db(range_km=550, elevation_deg=90)
    far = estimate_sinr_db(range_km=1200, elevation_deg=25)
    assert 10 < near < 30 and far < near
    assert abs((near - estimate_sinr_db(range_km=550, elevation_deg=90,
                                        interference_margin_db=5)) - 5) < 1e-9

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(
            raw={"sinr_model": {"enabled": True,
                                "starlink": {"eirp_dbw": 36.0},
                                "oneweb": {"eirp_dbw": 34.0}}},
            ground_station=GroundStation(), db_path=os.path.join(d, "t.sqlite"),
            collectors=[], netqual={"enabled": False},
            handover={"enabled": False}, webapp={})
        orch = Orchestrator(cfg)
        orch.start()
        try:
            orch._emit_sinr_model({
                "starlink": {"satellite": "SL-1", "elevation_deg": 62.0,
                             "azimuth_deg": 140.0, "range_km": 610.0},
                "oneweb": {"satellite": "OW-1", "elevation_deg": 48.0,
                           "azimuth_deg": 200.0, "range_km": 1450.0}})
            model = {r["source"]: r for r in orch.storage.recent_rf(0)
                     if r["kind"] == "model"}
            assert {"model:starlink", "model:oneweb"} <= set(model)
            assert model["model:starlink"]["sinr_db"] is not None

            c = create_app(orch.storage, orch).test_client()
            assert c.post("/api/markers",
                          json={"text": "距離 1.5m"}).get_json()["ok"]
            assert c.get("/api/markers?minutes=60").get_json()[0]["text"] == "距離 1.5m"
            assert "距離 1.5m" in c.get(
                "/export/markers.csv?minutes=60").data.decode("utf-8-sig")
        finally:
            orch.stop()
