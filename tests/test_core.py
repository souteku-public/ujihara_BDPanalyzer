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


def test_rain_attenuation_and_weather_integration():
    """降雨減衰モデルと気象モニタ → 理論 SINR への反映・定量記録."""
    from unittest.mock import patch, MagicMock
    from bdp_analyzer.handover.linkbudget import (rain_attenuation_db,
                                                  estimate_sinr_db)
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator
    from bdp_analyzer.webapp.server import create_app

    # 雨量・仰角に対して物理的に妥当な傾向
    assert rain_attenuation_db(0, 45) == 0.0
    a5, a25 = rain_attenuation_db(5, 45), rain_attenuation_db(25, 45)
    assert 0 < a5 < a25 < rain_attenuation_db(25, 20)
    assert estimate_sinr_db(range_km=600, elevation_deg=50, rain_mmh=25) \
        < estimate_sinr_db(range_km=600, elevation_deg=50) - 3

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(
            raw={"sinr_model": {"enabled": True, "use_weather": True,
                                "starlink": {"eirp_dbw": 36.0}},
                 "weather": {"enabled": True, "interval_s": 300}},
            ground_station=GroundStation(latitude=35.68, longitude=139.65),
            db_path=os.path.join(d, "t.sqlite"), collectors=[],
            netqual={"enabled": False}, handover={"enabled": False}, webapp={})

        fake = MagicMock()
        fake.json.return_value = {"current": {
            "temperature_2m": 24.5, "relative_humidity_2m": 88,
            "precipitation": 5.0, "rain": 5.0, "cloud_cover": 95,
            "weather_code": 63, "wind_speed_10m": 6.2}}
        fake.raise_for_status = lambda: None

        with patch("bdp_analyzer.weather.requests.get", return_value=fake):
            orch = Orchestrator(cfg)
            orch.start()
            try:
                time.sleep(0.5)
                wx = orch.storage.recent_weather(0)
                assert wx and wx[-1]["precip_mmh"] == 20.0   # 15分5mm → 20mm/h
                assert wx[-1]["rain_atten_ku45_db"] > 1.0

                serving = {"starlink": {"satellite": "SL",
                                        "elevation_deg": 50.0,
                                        "azimuth_deg": 100.0,
                                        "range_km": 600.0}}
                orch._emit_sinr_model(serving)          # 雨あり
                rain_val = [r for r in orch.storage.recent_rf(0)
                            if r["kind"] == "model"][-1]["sinr_db"]
                orch.latest_weather = {}
                orch._emit_sinr_model(serving)          # 晴天
                clear_val = [r for r in orch.storage.recent_rf(0)
                             if r["kind"] == "model"][-1]["sinr_db"]
                assert rain_val < clear_val - 3

                c = create_app(orch.storage, orch).test_client()
                assert c.get("/api/weather?minutes=60").get_json()[-1][
                    "precip_mmh"] == 20.0
                csv = c.get("/export/weather.csv?minutes=60"
                            ).data.decode("utf-8-sig")
                assert csv.splitlines()[0].startswith("ts,time_iso,precip_mmh")
            finally:
                orch.stop()


def test_skyplot_visible_and_next():
    """スカイプロット用: 可視衛星リストと次ハンドオーバー情報の付与."""
    from datetime import datetime, timezone
    from bdp_analyzer.config import GroundStation
    from bdp_analyzer.handover.predict import HandoverPredictor

    with tempfile.TemporaryDirectory() as d:
        tle = ("SAT-A\n"
               "1 25544U 98067A   24001.50000000  .00016717  00000-0  "
               "10270-3 0  9002\n"
               "2 25544  51.6400 208.9163 0002571  81.0000 279.1000 "
               "15.49386233000000\n"
               "SAT-B\n"
               "1 44238U 19029A   24001.50000000  .00001000  00000-0  "
               "10000-3 0  9995\n"
               "2 44238  53.0000 100.0000 0001000  90.0000 270.0000 "
               "15.05000000000000\n")
        with open(os.path.join(d, "tle_starlink.txt"), "w") as fh:
            fh.write(tle)
        epoch = datetime(2024, 1, 1, 12, 0, 0,
                         tzinfo=timezone.utc).timestamp()
        hcfg = {"horizon_min": 100, "step_s": 15,
                "constellations": ["starlink"],
                "tle_sources": {"starlink": "x"}, "tle_cache_hours": 999}
        gs = GroundStation(latitude=0, longitude=-180, altitude_m=0,
                           elevation_mask_deg=10)
        serving, events = HandoverPredictor(hcfg, gs, d).predict(epoch)
        info = serving["starlink"]
        assert info["visible"], "可視衛星リストが空"
        v = info["visible"][0]
        assert all(k in v for k in ("name", "el", "az", "range_km"))
        assert any(x["name"] == info["satellite"] for x in info["visible"])

    # orchestrator が next (次の切替先) を serving に付与する
    from bdp_analyzer.config import Config
    from bdp_analyzer.orchestrator import Orchestrator
    from bdp_analyzer.model import HandoverEvent

    class StubPredictor:
        def predict(self, now):
            return ({"starlink": {"satellite": "A", "elevation_deg": 50.0,
                                  "azimuth_deg": 10.0, "range_km": 600.0,
                                  "visible": []}},
                    [HandoverEvent(ts=now + 120, constellation="starlink",
                                   from_sat="A", to_sat="B",
                                   reason="better_candidate")])

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(raw={}, ground_station=GroundStation(),
                     db_path=os.path.join(d, "t.sqlite"), collectors=[],
                     netqual={"enabled": False}, handover={"enabled": False},
                     webapp={})
        orch = Orchestrator(cfg)
        orch.start()
        try:
            orch._handover_job(StubPredictor())
            nxt = orch.latest_serving["starlink"]["next"]
            assert nxt["to_sat"] == "B" and nxt["reason"] == "better_candidate"
            # sinr_model セクション無しでも既定で理論値が出る (回帰)
            assert any(r["kind"] == "model"
                       for r in orch.storage.recent_rf(0))
        finally:
            orch.stop()


def test_receiver_source_allowlist():
    """受信サーバの送信元 IP 制限: 許可外は TCP 拒否・UDP 無応答."""
    from bdp_analyzer.netqual.server import NetqualServer
    from bdp_analyzer.netqual import client

    srv = NetqualServer(15791, 15792, allowed_sources=["192.0.2.0/24"])
    srv.start()
    try:
        time.sleep(0.3)
        s = client.measure_once("127.0.0.1", 15791, 15792,
                                throughput_seconds=0.3, udp_probe_count=5,
                                udp_probe_interval_ms=5, timeout=2)
        assert not s[0].throughput_bps          # 許可外 → 測定不成立
        assert s[0].loss_pct == 100.0
    finally:
        srv.stop()
    time.sleep(0.5)

    srv2 = NetqualServer(15793, 15794, allowed_sources=["127.0.0.1/32"])
    srv2.start()
    try:
        time.sleep(0.3)
        s2 = client.measure_once("127.0.0.1", 15793, 15794,
                                 throughput_seconds=0.3, udp_probe_count=5,
                                 udp_probe_interval_ms=5)
        assert s2[0].throughput_bps > 0         # 許可内 → 正常測定
        assert s2[0].rtt_ms is not None
    finally:
        srv2.stop()


def test_soak_test_and_serverstats():
    """耐久テスト (一定レート×一定時間) と受信サーバ状態 API."""
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator
    from bdp_analyzer.webapp.server import create_app

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(
            raw={}, ground_station=GroundStation(),
            db_path=os.path.join(d, "t.sqlite"), collectors=[],
            netqual={"enabled": True, "role": "both",
                     "control_port": 15811, "udp_port": 15812,
                     "interval_s": 600,
                     "targets": [{"label": "lo", "host": "127.0.0.1",
                                  "enabled": False}]},
            handover={"enabled": False}, webapp={})
        orch = Orchestrator(cfg)
        orch.start()
        try:
            c = create_app(orch.storage, orch).test_client()
            r = c.post("/api/loadtest", json={
                "type": "soak", "target": "net:lo", "direction": "uplink",
                "rate_mbps": 3, "duration_s": 2, "chunk_s": 1})
            assert r.get_json()["ok"]
            for _ in range(30):
                st = c.get("/api/loadtest").get_json()
                if not st["running"]:
                    break
                time.sleep(0.5)
            assert st["error"] is None and st["mode"] == "soak"
            assert len(st["results"]) == 2      # 2 秒 / 1 秒チャンク
            assert all(abs(x["offered_bps"] - 3e6) < 1 for x in st["results"])
            assert all(x["achieved_bps"] > 2e6 for x in st["results"])

            # 合計 1 時間超は拒否
            r = c.post("/api/loadtest", json={
                "type": "soak", "target": "net:lo", "direction": "uplink",
                "rate_mbps": 3, "duration_s": 7200})
            assert not r.get_json()["ok"]

            s = c.get("/api/serverstats").get_json()
            assert s["running"] and s["control_port"] == 15811
            assert s["udp_counts"]["load"] > 0
        finally:
            orch.stop()


def test_iperf_like_features():
    """iperf 相当機能: 並列ストリーム合算・omit・順序逆転の記録."""
    from bdp_analyzer.netqual import client
    from bdp_analyzer.netqual.server import NetqualServer

    # 並列合算ロジック (実回線の帯域制限を模擬したユニット検証)
    assert client._parallel_bps(lambda *a: 25e6, 1) == 25e6
    n = {"c": 0}

    def stub(*a):
        n["c"] += 1
        return 25e6
    assert client._parallel_bps(stub, 4) == 100e6 and n["c"] == 4
    # 失敗ストリーム (None) は除外して合算
    seq = iter([10e6, None, 10e6, None])
    assert client._parallel_bps(lambda *a: next(seq), 4) == 20e6

    # ループバック統合: streams メタ・omit 完走・out_of_order 記録
    srv = NetqualServer(15831, 15832)
    srv.start()
    try:
        time.sleep(0.3)
        s = client.measure_once("127.0.0.1", 15831, 15832,
                                throughput_seconds=2, udp_probe_count=20,
                                udp_probe_interval_ms=5, streams=4,
                                omit_seconds=1)
        assert s[0].streams == 4 and s[1].streams == 4
        assert s[0].throughput_bps > 0 and s[1].throughput_bps > 0
        assert s[0].out_of_order_pct is not None   # ループバックは通常 0
    finally:
        srv.stop()


def test_net_samples_migration_adds_columns():
    """既存 (旧スキーマ) DB に out_of_order_pct / streams 列が追加される."""
    import sqlite3
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.sqlite")
        con = sqlite3.connect(path)
        # 直前リリースの net_samples スキーマ (out_of_order_pct / streams が無い)
        con.execute("CREATE TABLE net_samples (id INTEGER PRIMARY KEY "
                    "AUTOINCREMENT, ts REAL NOT NULL, session TEXT, "
                    "direction TEXT, throughput_bps REAL, rtt_ms REAL, "
                    "rtt_min_ms REAL, rtt_max_ms REAL, jitter_ms REAL, "
                    "loss_pct REAL, owd_ms REAL)")
        con.commit()
        con.close()
        st = Storage(path)                          # マイグレーションが走る
        st.add_net(NetSample(ts=time.time(), session="x", direction="downlink",
                             out_of_order_pct=1.5, streams=4))
        row = st.recent_net(0)[0]
        assert row["out_of_order_pct"] == 1.5 and row["streams"] == 4


def test_iperf3_backend_and_fallback():
    """iperf3 バックエンド委譲と、iperf3 不在時の内蔵フォールバック."""
    import shutil
    from bdp_analyzer.netqual.server import NetqualServer
    from bdp_analyzer.netqual import client, iperf

    # iperf3 が無い環境ではフォールバックのみ検証
    srv = NetqualServer(15921, 15922,
                        iperf_port=5311 if iperf.available() else 0)
    srv.start()
    try:
        time.sleep(1.0 if iperf.available() else 0.3)
        # engine=iperf3 指定 (無ければ内蔵に自動フォールバック) → 必ず値が出る
        s = client.measure_once("127.0.0.1", 15921, 15922, throughput_seconds=1,
                                udp_probe_count=10, udp_probe_interval_ms=5,
                                streams=2, engine="iperf3", iperf_port=5311)
        assert s[0].throughput_bps and s[0].throughput_bps > 0
        assert s[0].rtt_ms is not None          # RTT は engine に関わらず内蔵測定
        # 存在しないバイナリ名 → 明示的に内蔵フォールバック
        s2 = client.measure_once("127.0.0.1", 15921, 15922, throughput_seconds=1,
                                 udp_probe_count=10, udp_probe_interval_ms=5,
                                 engine="iperf3", iperf_bin="iperf3_nope")
        assert s2[0].throughput_bps and s2[0].throughput_bps > 0
    finally:
        srv.stop()

    if iperf.available():
        assert iperf.throughput_bps  # モジュール健全性


def test_ingest_and_local_csv():
    """インストール制限の送信 PC 想定: /api/ingest 中央記録 + ローカル CSV 出力."""
    import json
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator
    from bdp_analyzer.webapp.server import create_app
    from bdp_analyzer.__main__ import _append_net_csv

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(raw={}, ground_station=GroundStation(),
                     db_path=os.path.join(d, "rx.sqlite"), collectors=[],
                     netqual={"enabled": False, "ingest_token": "secret"},
                     handover={"enabled": False}, webapp={})
        orch = Orchestrator(cfg)
        orch.start()
        try:
            c = create_app(orch.storage, orch).test_client()
            payload = {"token": "secret", "samples": [
                {"ts": time.time(), "session": "sat", "direction": "downlink",
                 "throughput_bps": 5e7, "rtt_ms": 40, "streams": 4},
                {"ts": time.time(), "session": "sat", "direction": "uplink",
                 "throughput_bps": 8e6}]}
            r = c.post("/api/ingest", json=payload)
            assert r.get_json()["stored"] == 2
            assert len(orch.storage.recent_net(0)) == 2
            # token 不一致 → 403
            bad = c.post("/api/ingest", json={"token": "x", "samples": payload["samples"]})
            assert bad.status_code == 403
        finally:
            orch.stop()

        # ローカル CSV 追記 (stdlib のみ、ヘッダ + 追記)
        from bdp_analyzer.model import NetSample
        csv_path = os.path.join(d, "out.csv")
        s = [NetSample(ts=time.time(), session="sat", direction="downlink",
                       throughput_bps=5e7, streams=4)]
        _append_net_csv(csv_path, s)
        _append_net_csv(csv_path, s)   # 追記 (ヘッダは 1 回だけ)
        lines = open(csv_path, encoding="utf-8-sig").read().splitlines()
        assert lines[0].startswith("ts,time_iso,session,direction,throughput_bps")
        assert len(lines) == 3         # header + 2 data rows


def test_inetspeed_single_terminal():
    """単独端末インターネット速度測定 (受信側 PC 不要, 公開エンドポイント方式)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from bdp_analyzer.netqual import inetspeed

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            n = 2_000_000
            self.send_response(200)
            self.send_header("Content-Length", str(n))
            self.end_headers()
            b = b"x" * 65536
            s = 0
            while s < n:
                try:
                    self.wfile.write(b[:min(65536, n - s)])
                except OSError:
                    break
                s += 65536

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            while ln > 0:
                d = self.rfile.read(min(65536, ln))
                if not d:
                    break
                ln -= len(d)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ep = {"down_url": f"http://127.0.0.1:{port}/__down?bytes=",
              "up_url": f"http://127.0.0.1:{port}/__up",
              "rtt_host": "127.0.0.1", "rtt_port": port,
              "down_bytes": 2_000_000, "up_chunk": 500_000}
        s = inetspeed.measure_public(endpoints=ep, seconds=1, streams=2,
                                     session="net")
        assert len(s) == 2
        assert s[0].direction == "downlink" and s[0].throughput_bps > 0
        assert s[1].direction == "uplink" and s[1].throughput_bps > 0
        assert s[0].rtt_ms is not None and s[0].streams == 2
    finally:
        srv.shutdown()


def test_simple_app_poll_graph_and_csv_record():
    """シンプル計測: 5秒ポーリング相当 + UI からの CSV 記録開始/停止."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from bdp_analyzer.simpleapp.server import SimpleMonitor, create_simple_app

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({
                "rf": {"sinr": 12.5, "tx_frequency_mhz": 14250,
                       "rx_frequency_mhz": 11700},
                "pointing": {"azimuth": 181.0, "elevation": 42.0},
                "network": {"satellite": "OW-0042"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as d:
        mon = SimpleMonitor(
            {"name": "kymeta-test", "kind": "kymeta",
             "base_url": f"http://127.0.0.1:{port}",
             "endpoints": {"status_json": "/api/status"},
             "field_map": {"sinr_db": "rf.sinr",
                           "azimuth_deg": "pointing.azimuth",
                           "elevation_deg": "pointing.elevation",
                           "tx_freq_mhz": "rf.tx_frequency_mhz",
                           "satellite_id": "network.satellite"}},
            interval_s=0.5, records_dir=os.path.join(d, "records"))
        mon.start()
        try:
            c = create_simple_app(mon).test_client()
            time.sleep(1.5)
            # ステータス / 時系列 (グラフ用データ) が取れている
            st = c.get("/api/simple/status").get_json()
            assert st["name"] == "kymeta-test" and st["last_ts"] is not None
            rows = c.get("/api/simple/series?minutes=5").get_json()
            assert rows and rows[-1]["sinr_db"] == 12.5
            assert rows[-1]["satellite_id"] == "OW-0042"

            # 記録開始 → 行が増える → 停止でファイル確定
            rec = c.post("/api/simple/record",
                         json={"action": "start"}).get_json()
            assert rec["active"] and rec["path"]
            time.sleep(1.6)
            rec2 = c.get("/api/simple/status").get_json()["recording"]
            assert rec2["rows"] >= 2
            rec3 = c.post("/api/simple/record",
                          json={"action": "stop"}).get_json()
            assert not rec3["active"] and rec3["last_path"]
            lines = open(rec3["last_path"],
                         encoding="utf-8-sig").read().splitlines()
            assert lines[0].startswith("ts,time_iso,sinr_db")
            assert len(lines) >= 3
            assert "12.5" in lines[1] and "OW-0042" in lines[1]
            # 記録 CSV をダウンロードできる
            dl = c.get("/record.csv")
            assert dl.status_code == 200
        finally:
            mon.stop()
            srv.shutdown()


def test_webgui_probe_discovers_endpoint_and_fields():
    """probe: 実機 WebGUI を探索し endpoints/field_map を自動提案できる."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from bdp_analyzer.collectors.probe import WebGuiProber

    HTML = ('<html><body><script src="/main.js"></script>'
            '<script>fetch("/api/modem/status").then(r=>r.json())</script>'
            '</body></html>')
    STATUS = {"rf": {"sinr": 12.4, "rx_frequency": 11700.0,
                     "tx_frequency": 14250.0},
              "pointing": {"elevation": 41.2, "azimuth": 183.5},
              "network": {"satellite": "OW-0231"}}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                body, ct = HTML.encode(), "text/html"
            elif p == "/main.js":
                body, ct = b'var u="/api/rf";', "application/javascript"
            elif p == "/api/modem/status":
                body, ct = json.dumps(STATUS).encode(), "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rep = WebGuiProber(f"http://127.0.0.1:{port}").run()
        assert rep["suggest"]["status_json"] == "/api/modem/status"
        fm = rep["suggest"]["field_map"]
        assert fm["sinr_db"] == "rf.sinr"
        assert fm["elevation_deg"] == "pointing.elevation"
        assert fm["azimuth_deg"] == "pointing.azimuth"
        assert fm["satellite_id"] == "network.satellite"
    finally:
        srv.shutdown()


def test_add_internet_target_via_api():
    """UI から受信側PC不要の『インターネット速度』測定先を追加できる (host 不要)."""
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator
    from bdp_analyzer.webapp.server import create_app

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(raw={}, ground_station=GroundStation(),
                     db_path=os.path.join(d, "t.sqlite"), collectors=[],
                     handover={"enabled": False}, webapp={},
                     netqual={"enabled": True, "role": "sender", "targets": []})
        orch = Orchestrator(cfg)
        orch.start()
        try:
            c = create_app(orch.storage, orch).test_client()
            # host なし・mode:internet で追加できる
            r = c.post("/api/targets", json={"label": "OneWeb経由",
                                             "mode": "internet", "streams": 4})
            assert r.get_json()["ok"]
            jobs = {j["id"]: j for j in orch.jobs_status()}
            assert "net:OneWeb経由" in jobs
            # 公開速度測定に RTT モニタは付かない
            assert not any(j.startswith("rttmon:") for j in jobs)
            # host 無し・mode 無しは従来通り拒否される
            bad = c.post("/api/targets", json={"label": "x"})
            assert bad.status_code == 400
        finally:
            orch.stop()

        # 再起動後も mode:internet の測定先が復元される
        orch2 = Orchestrator(cfg)
        orch2.start()
        try:
            j2 = {j["id"]: j for j in orch2.jobs_status()}
            assert "net:OneWeb経由" in j2
        finally:
            orch2.stop()


def test_measurement_bind_pinning_and_validation():
    """有線固定: グローバル bind_ip が全測定に既定適用され、個別が優先される."""
    from bdp_analyzer.config import Config, GroundStation
    from bdp_analyzer.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(raw={}, ground_station=GroundStation(),
                     db_path=os.path.join(d, "t.sqlite"), collectors=[],
                     handover={"enabled": False}, webapp={},
                     netqual={"enabled": True, "role": "sender",
                              "bind_ip": "192.0.2.55", "targets": []})
        orch = Orchestrator(cfg)
        # グローバル既定 / 測定先個別が優先 / mode:internet でも同様
        assert orch._eff_bind({"host": "10.0.0.1"}) == "192.0.2.55"
        assert orch._eff_bind({"bind_ip": "10.9.9.9"}) == "10.9.9.9"
        assert orch._eff_bind({"mode": "internet"}) == "192.0.2.55"
        # bind 無し設定なら None (従来どおり OS ルート任せ)
        cfg2 = Config(raw={}, ground_station=GroundStation(),
                      db_path=os.path.join(d, "t2.sqlite"), collectors=[],
                      handover={"enabled": False}, webapp={},
                      netqual={"enabled": True, "role": "sender", "targets": []})
        assert Orchestrator(cfg2)._eff_bind({"host": "x"}) is None


def test_server_listen_bind():
    """受信サーバの待受を特定 NIC に限定 (本社: 固定IPのみで応答, Wi-Fi不応答)."""
    import socket
    from bdp_analyzer.netqual.server import NetqualServer
    from bdp_analyzer.netqual import client

    srv = NetqualServer(16211, 16212, listen_ip="127.0.0.1")
    srv.start()
    try:
        time.sleep(0.4)
        s = client.measure_once("127.0.0.1", 16211, 16212, throughput_seconds=0.4,
                                udp_probe_count=8, udp_probe_interval_ms=5,
                                bind_ip="127.0.0.1")
        assert s[0].throughput_bps > 0 and s[0].rtt_ms is not None
    finally:
        srv.stop()
    time.sleep(0.3)

    # 別の実在 IP に待受を限定すると 127.0.0.1 宛では接続不可
    srv2 = NetqualServer(16213, 16214, listen_ip="192.0.2.2")
    srv2.start()
    try:
        time.sleep(0.4)
        try:
            socket.create_connection(("127.0.0.1", 16213), timeout=1).close()
            assert False, "指定NIC以外で待ち受けてしまった"
        except OSError:
            pass
    finally:
        srv2.stop()
