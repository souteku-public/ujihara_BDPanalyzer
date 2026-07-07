"""SINR 理論値の推定 (簡易リンクバジェット).

TLE/SGP4 で得た「接続中と推定される衛星」の距離・仰角から、ダウンリンクの
理論 SNR を推定する:

    C/N0 [dBHz] = EIRP [dBW] + G/T [dB/K] − FSPL [dB] − L_misc − L_atm + 228.6
    SNR  [dB]   = C/N0 − 10·log10(帯域幅 [Hz]) − 干渉マージン

    FSPL = 92.45 + 20·log10(距離 [km]) + 20·log10(周波数 [GHz])
    L_atm = 天頂大気損失 / sin(仰角)   (仰角が低いほど大気路程が伸びる近似)

注意 (使い方の前提):
  - 衛星 EIRP や端末 G/T の正確な値は事業者非公開のため、既定値は
    「桁が合う程度」の代表値。**絶対値ではなく傾向 (時間変化・条件差) を
    見る道具**として使い、晴天・無干渉の基準状態で実測と合うように
    eirp_dbw か losses_db をキャリブレーションするのが正しい使い方。
  - キャリブレーション後の「実測 − 理論」の差分 (Δ) が、アンテナ間干渉・
    降雨減衰・遮蔽などの劣化量の推定になる。
"""
from __future__ import annotations

import math

BOLTZMANN_TERM_DB = 228.6   # −10·log10(k), k: ボルツマン定数

# ITU-R P.838-3 の周波数別係数 (水平偏波)。γ = k·R^α [dB/km], R: 降雨強度 mm/h
_P838 = [
    (10.0, 0.01217, 1.2571),
    (12.0, 0.02386, 1.1825),
    (14.0, 0.04481, 1.1233),
    (20.0, 0.09164, 1.0568),
]


def fspl_db(range_km: float, freq_ghz: float) -> float:
    """自由空間伝搬損失 [dB]."""
    return 92.45 + 20.0 * math.log10(max(range_km, 1.0)) \
                 + 20.0 * math.log10(max(freq_ghz, 0.001))


def _p838_coeffs(freq_ghz: float) -> tuple[float, float]:
    """P.838 係数 k, α を周波数で補間 (範囲外は端でクランプ)."""
    pts = _P838
    if freq_ghz <= pts[0][0]:
        return pts[0][1], pts[0][2]
    if freq_ghz >= pts[-1][0]:
        return pts[-1][1], pts[-1][2]
    for (f1, k1, a1), (f2, k2, a2) in zip(pts, pts[1:]):
        if f1 <= freq_ghz <= f2:
            t = (freq_ghz - f1) / (f2 - f1)
            k = math.exp(math.log(k1) + t * (math.log(k2) - math.log(k1)))
            return k, a1 + t * (a2 - a1)
    return pts[-1][1], pts[-1][2]


def rain_attenuation_db(rain_mmh: float, elevation_deg: float,
                        freq_ghz: float = 11.7,
                        rain_height_km: float = 4.5) -> float:
    """降雨減衰 [dB] の簡易推定 (ITU-R P.838 + P.618 の簡略形).

    γ = k·R^α [dB/km] に、雨域 (高さ rain_height_km) を貫く斜め路長と
    水平方向のセル有限性を表す簡易リダクション係数 r = 35/(35+L_G) を掛ける。
    日本の中緯度では rain_height_km ≈ 4〜5 km が目安。
    """
    if rain_mmh <= 0:
        return 0.0
    el = max(elevation_deg, 5.0)
    k, alpha = _p838_coeffs(freq_ghz)
    gamma = k * (rain_mmh ** alpha)                       # dB/km
    slant_km = rain_height_km / math.sin(math.radians(el))
    ground_km = rain_height_km / math.tan(math.radians(el))
    reduction = 35.0 / (35.0 + ground_km)
    return gamma * slant_km * reduction


def estimate_sinr_db(*, range_km: float, elevation_deg: float,
                     freq_ghz: float = 11.7,
                     eirp_dbw: float = 36.0,
                     gt_dbk: float = 9.0,
                     bandwidth_mhz: float = 240.0,
                     losses_db: float = 1.0,
                     atmos_zenith_db: float = 0.5,
                     interference_margin_db: float = 0.0,
                     rain_mmh: float = 0.0,
                     rain_height_km: float = 4.5) -> float:
    """接続衛星の距離・仰角から理論 SINR [dB] を推定する.

    rain_mmh (降水強度) を与えると ITU-R P.838 ベースの降雨減衰を差し引く。
    """
    atm = atmos_zenith_db / max(math.sin(math.radians(max(elevation_deg, 5.0))), 0.1)
    rain = rain_attenuation_db(rain_mmh, elevation_deg, freq_ghz, rain_height_km)
    cn0 = (eirp_dbw + gt_dbk - fspl_db(range_km, freq_ghz)
           - losses_db - atm - rain + BOLTZMANN_TERM_DB)
    snr = cn0 - 10.0 * math.log10(bandwidth_mhz * 1e6)
    return snr - interference_margin_db


# estimate_sinr_db に渡してよい設定キー (config の sinr_model.<constellation>)
PARAM_KEYS = ("freq_ghz", "eirp_dbw", "gt_dbk", "bandwidth_mhz",
              "losses_db", "atmos_zenith_db", "interference_margin_db",
              "rain_height_km")
