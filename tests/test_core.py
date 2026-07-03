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
