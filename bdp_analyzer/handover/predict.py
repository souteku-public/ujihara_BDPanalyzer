"""ハンドオーバー予測本体.

考え方(ヒューリスティック):
  実際の Starlink / OneWeb は非公開のスケジューラで衛星を割り当てるため、
  地上からは「どの衛星に繋がっているか」を厳密には知り得ない。そこで本モジュールは
  『自局から見て最も仰角の高い(=最も条件の良い)可視衛星がサービス衛星』という
  一般的な近似を用い、時間を先送りしながらサービス衛星が入れ替わる時刻を
  ハンドオーバー予測として出力する。

  併せて、現在のサービス衛星が仰角マスクを割り込む時刻も算出する。
  これにより「あと N 秒でハンドオーバーが起きそう」という先読みができる。

依存: skyfield (+ numpy)。未インストール時は空結果を返し、本体は動作継続。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import GroundStation
from ..model import HandoverEvent
from .tle import fetch_tle

log = logging.getLogger("bdp.handover.predict")


class HandoverPredictor:
    def __init__(self, hcfg: Dict[str, Any], gs: GroundStation, cache_dir: str):
        self.cfg = hcfg
        self.gs = gs
        self.cache_dir = cache_dir
        self.horizon_s = float(hcfg.get("horizon_min", 15)) * 60.0
        self.step_s = float(hcfg.get("step_s", 15))
        self.mask = float(gs.elevation_mask_deg)
        self.constellations: List[str] = hcfg.get("constellations", ["starlink", "oneweb"])
        self.tle_sources: Dict[str, str] = hcfg.get("tle_sources", {})
        self.max_satellites = int(hcfg.get("max_satellites", 0))  # 0 = 無制限

        self._ts = None
        self._observer = None
        self._sats: Dict[str, list] = {}
        self._loaded_at = 0.0
        self._ok = self._init_skyfield()

    def _init_skyfield(self) -> bool:
        try:
            from skyfield.api import load, wgs84  # type: ignore
        except Exception:  # noqa: BLE001
            log.error("skyfield 未インストールのためハンドオーバー予測は無効です "
                      "(`pip install skyfield numpy`)")
            return False
        self._load = load
        self._wgs84 = wgs84
        self._ts = load.timescale()
        self._observer = wgs84.latlon(self.gs.latitude, self.gs.longitude,
                                      elevation_m=self.gs.altitude_m)
        return True

    # ---- 衛星ロード ------------------------------------------------------
    def _ensure_satellites(self) -> None:
        cache_hours = float(self.cfg.get("tle_cache_hours", 6))
        if self._sats and (time.time() - self._loaded_at) / 3600.0 < cache_hours:
            return
        from skyfield.api import EarthSatellite  # type: ignore
        self._sats = {}
        for con in self.constellations:
            url = self.tle_sources.get(con)
            if not url:
                continue
            tles = fetch_tle(con, url, self.cache_dir, cache_hours)
            if self.max_satellites:
                tles = tles[: self.max_satellites]
            sats = [EarthSatellite(l1, l2, name, self._ts) for (name, l1, l2) in tles]
            self._sats[con] = sats
            log.info("ロード衛星数 %s: %d", con, len(sats))
        self._loaded_at = time.time()

    # ---- 予測 ------------------------------------------------------------
    def predict(self, now_ts: Optional[float] = None
                ) -> Tuple[Dict[str, Any], List[HandoverEvent]]:
        """(現在のサービス衛星情報, ハンドオーバー予測イベント) を返す."""
        if not self._ok:
            return {}, []
        try:
            self._ensure_satellites()
        except Exception as e:  # noqa: BLE001
            log.warning("衛星ロード失敗: %s", e)
            return {}, []

        now_ts = now_ts or time.time()
        n_steps = max(2, int(self.horizon_s / self.step_s) + 1)
        # skyfield の Time 配列 (now から horizon まで step_s 刻み)
        import numpy as np  # type: ignore
        from datetime import datetime, timezone, timedelta
        base = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        times = self._ts.from_datetimes(
            [base + timedelta(seconds=i * self.step_s) for i in range(n_steps)])

        serving_now: Dict[str, Any] = {}
        events: List[HandoverEvent] = []

        for con, sats in self._sats.items():
            if not sats:
                continue
            # 各衛星の仰角(度)を時間配列で一括計算し、行列 alt[sat][step] を作る
            alt_rows = []
            az_rows = []
            dist_rows = []
            names = []
            for sat in sats:
                topo = (sat - self._observer).at(times)
                alt, az, dist = topo.altaz()
                a = alt.degrees
                if float(np.max(a)) < self.mask:
                    continue  # この窓では一度も可視にならない
                alt_rows.append(a)
                az_rows.append(az.degrees)
                dist_rows.append(dist.km)
                names.append(sat.name)
            if not alt_rows:
                serving_now[con] = None
                continue

            alt_mat = np.vstack(alt_rows)          # shape (S, T)
            az_mat = np.vstack(az_rows)
            dist_mat = np.vstack(dist_rows)
            masked = np.where(alt_mat >= self.mask, alt_mat, -1.0)

            # 各時刻でサービス衛星 = 仰角最大。可視ゼロなら -1。
            serving_idx = np.argmax(masked, axis=0)
            serving_vis = masked.max(axis=0) >= 0

            # 現在(step 0)のサービス衛星
            if serving_vis[0]:
                i0 = int(serving_idx[0])
                serving_now[con] = {
                    "satellite": names[i0],
                    "elevation_deg": round(float(alt_mat[i0, 0]), 2),
                    "azimuth_deg": round(float(az_mat[i0, 0]), 2),
                    "range_km": round(float(dist_mat[i0, 0]), 1),
                    "visible_count": int((masked[:, 0] >= 0).sum()),
                }
            else:
                serving_now[con] = {"satellite": None, "visible_count": 0}

            # タイムラインを走査してサービス衛星の切替=ハンドオーバーを検出
            prev = int(serving_idx[0]) if serving_vis[0] else -1
            for step in range(1, n_steps):
                cur = int(serving_idx[step]) if serving_vis[step] else -1
                if cur == prev:
                    continue
                ev_ts = now_ts + step * self.step_s
                from_name = names[prev] if prev >= 0 else None
                to_name = names[cur] if cur >= 0 else None
                reason = "los" if to_name is None else (
                    "acquisition" if from_name is None else "better_candidate")
                # from 衛星がマスク割れならその理由を優先
                if from_name is not None and prev >= 0 and alt_mat[prev, step] < self.mask:
                    reason = "elevation_mask"
                events.append(HandoverEvent(
                    ts=ev_ts, constellation=con, from_sat=from_name, to_sat=to_name,
                    reason=reason,
                    from_elevation_deg=round(float(alt_mat[prev, step]), 2) if prev >= 0 else None,
                    to_elevation_deg=round(float(alt_mat[cur, step]), 2) if cur >= 0 else None,
                    lead_time_s=round(step * self.step_s, 1),
                ))
                prev = cur

        events.sort(key=lambda e: e.ts)
        return serving_now, events
