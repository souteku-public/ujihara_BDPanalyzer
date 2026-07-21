"""Kymeta / 汎用 WebGUI 探索ツール.

実機の WebGUI がどんな URL で RF 情報 (SINR・仰角・方位角・周波数など) を
返すのかは機種・ファームで異なる。config の ``endpoints`` / ``field_map`` を
正しく設定するために、実機へ実際にアクセスして

  1. トップページ HTML から API/JSON らしき URL を抽出
  2. よくあるエンドポイント候補と合わせて順にアクセスし、応答を分類
  3. JSON 応答はドットパスに平坦化し、RF らしきキーを自動抽出
  4. ``endpoints.status_json`` と ``field_map`` の推奨値を提示

を行う。結果を見て config.yaml を調整する。実機に到達できる PC で実行すること。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import requests

# トップ HTML / JS から拾えなくても試す、よくある候補パス
_CANDIDATES = [
    "/api/status", "/api/v1/status", "/api/status.json", "/status",
    "/status.json", "/api/system", "/api/system/status", "/api/modem",
    "/api/modem/status", "/api/terminal", "/api/terminal/status",
    "/api/statistics", "/api/stats", "/api/rf", "/api/pointing",
    "/api/antenna", "/api/gps", "/api/state", "/api/info", "/api/summary",
    "/data/status.json", "/cgi-bin/status", "/json", "/state.json",
    "/api/v1/modem/status", "/api/v1/system", "/rest/status",
]

# RF フィールド → キー名に含まれうる語 (小文字比較, 順に優先)
_FIELD_HINTS = {
    "sinr_db": ["sinr", "snr", "cno", "cn0", "c_n", "esn0", "es_n0"],
    "elevation_deg": ["elevation", "elev", "el_deg", "look_el", "boresight_el"],
    "azimuth_deg": ["azimuth", "azim", "az_deg", "look_az", "boresight_az"],
    "tx_freq_mhz": ["tx_freq", "txfreq", "tx_frequency", "uplink_freq", "ul_freq"],
    "rx_freq_mhz": ["rx_freq", "rxfreq", "rx_frequency", "downlink_freq", "dl_freq"],
    "satellite_id": ["satellite", "sat_id", "satid", "beam", "beam_id", "nid"],
    "rssi_dbm": ["rssi", "power_dbm", "rx_power"],
    "tilt_deg": ["tilt", "skew", "polarization"],
}

_KEYWORDS = sorted({w for ws in _FIELD_HINTS.values() for w in ws})


def _flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """ネストした dict/list を "a.b.0.c" 形式のドットパスに平坦化する."""
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):   # 長い配列は先頭のみ
            out.update(_flatten(v, f"{prefix}.{i}"))
    else:
        out[prefix] = obj
    return out


def _extract_paths(text: str) -> List[str]:
    """HTML/JS から API/JSON らしき相対パスを抽出する."""
    paths = set()
    for pat in (
        r"""["'`](/[A-Za-z0-9_\-./]*\.json[A-Za-z0-9_\-./?=&]*)["'`]""",
        r"""["'`](/api/[A-Za-z0-9_\-./]*)["'`]""",
        r"""["'`](/rest/[A-Za-z0-9_\-./]*)["'`]""",
        r"""fetch\(\s*["'`]([^"'`]+)["'`]""",
        r"""\.(?:get|post)\(\s*["'`]([^"'`]+)["'`]""",
        r"""url\s*[:=]\s*["'`]([^"'`]+)["'`]""",
    ):
        for m in re.findall(pat, text):
            p = m.split("?")[0]
            if p.startswith("/") and not p.endswith((".js", ".css", ".png",
                                                     ".svg", ".ico", ".woff",
                                                     ".woff2", ".map")):
                paths.add(p)
    return sorted(paths)


class WebGuiProber:
    def __init__(self, base_url: str, *, username: Optional[str] = None,
                 password: Optional[str] = None, verify_tls: bool = False,
                 timeout_s: float = 8.0):
        self.base = base_url.rstrip("/")
        self.verify = verify_tls
        self.timeout = timeout_s
        self.session = requests.Session()
        if username is not None:
            self.session.auth = (username, password or "")
        if not verify_tls:
            requests.packages.urllib3.disable_warnings()  # type: ignore

    def _get(self, path: str) -> Tuple[Optional[requests.Response], str]:
        try:
            r = self.session.get(self.base + path, verify=self.verify,
                                  timeout=self.timeout)
            return r, ""
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def run(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"base": self.base, "json_endpoints": [],
                                  "other": [], "suggest": {}, "errors": []}

        # 1) トップ HTML を取得し、API パス候補を抽出
        discovered: List[str] = []
        root, err = self._get("/")
        if root is not None:
            report["root_status"] = root.status_code
            report["root_ctype"] = root.headers.get("Content-Type", "")
            html = root.text or ""
            discovered = _extract_paths(html)
            # 参照している .js も 3 本まで覗いて API パスを拾う
            for js in re.findall(r"""<script[^>]+src=["']([^"']+\.js)["']""",
                                 html)[:3]:
                jr, _ = self._get(js if js.startswith("/") else "/" + js)
                if jr is not None and jr.ok:
                    discovered += _extract_paths(jr.text or "")
        else:
            report["errors"].append(f"/ 取得失敗: {err}")

        # 2) 候補 (発見分 + 定番) を順に叩いて分類
        seen = set()
        for path in list(dict.fromkeys(discovered + _CANDIDATES)):
            if path in seen:
                continue
            seen.add(path)
            r, err = self._get(path)
            if r is None:
                continue
            ctype = r.headers.get("Content-Type", "")
            entry = {"path": path, "status": r.status_code,
                     "ctype": ctype, "bytes": len(r.content)}
            data = None
            if r.ok and ("json" in ctype.lower() or (r.text or "").lstrip()[:1]
                         in ("{", "[")):
                try:
                    data = r.json()
                except Exception:  # noqa: BLE001
                    data = None
            if data is not None:
                flat = _flatten(data)
                hits = {k: v for k, v in flat.items()
                        if any(w in k.lower() for w in _KEYWORDS)}
                entry["keys"] = len(flat)
                entry["rf_hits"] = hits
                report["json_endpoints"].append(entry)
            elif r.status_code != 404:
                report["other"].append(entry)

        # 3) 最も RF ヒットが多い JSON を採用し field_map を提案
        best = max(report["json_endpoints"],
                   key=lambda e: len(e["rf_hits"]), default=None)
        if best and best["rf_hits"]:
            report["suggest"]["status_json"] = best["path"]
            fm: Dict[str, str] = {}
            used = set()
            for field, hints in _FIELD_HINTS.items():
                for hint in hints:
                    cand = [k for k in best["rf_hits"]
                            if hint in k.lower() and k not in used]
                    if cand:
                        key = min(cand, key=len)   # 最短キーを採用
                        fm[field] = key
                        used.add(key)
                        break
            report["suggest"]["field_map"] = fm
        return report


def format_report(rep: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append(f"探索対象: {rep['base']}")
    if "root_status" in rep:
        L.append(f"  トップ / : HTTP {rep['root_status']} "
                 f"({rep.get('root_ctype', '')})")
    for e in rep.get("errors", []):
        L.append(f"  ! {e}")
    L.append("")
    L.append(f"=== JSON を返すエンドポイント ({len(rep['json_endpoints'])} 件) ===")
    if not rep["json_endpoints"]:
        L.append("  (見つからず。認証や TLS、または端末が JSON API 非対応の可能性)")
    for e in sorted(rep["json_endpoints"],
                    key=lambda x: len(x["rf_hits"]), reverse=True):
        L.append(f"  {e['path']}  HTTP {e['status']}  "
                 f"{e['keys']} keys  RFヒット {len(e['rf_hits'])}")
        for k, v in list(e["rf_hits"].items())[:12]:
            L.append(f"      {k} = {v}")
    if rep.get("other"):
        L.append("")
        L.append("=== JSON 以外で応答したパス (参考) ===")
        for e in rep["other"][:15]:
            L.append(f"  {e['path']}  HTTP {e['status']}  "
                     f"{e['ctype']}  {e['bytes']}B")

    sug = rep.get("suggest", {})
    L.append("")
    L.append("=== 推奨設定 (config.yaml の collectors 該当ブロックへ) ===")
    if sug.get("status_json"):
        L.append("    endpoints:")
        L.append(f"      status_json: \"{sug['status_json']}\"")
        L.append("    field_map:")
        for field, key in sug.get("field_map", {}).items():
            L.append(f"      {field}: \"{key}\"")
        if not sug.get("field_map"):
            L.append("      # RF らしきキーを自動判定できず。上記の JSON キー一覧から手動で対応づけてください")
    else:
        L.append("  自動判定できませんでした。上の一覧や、ブラウザの開発者ツール")
        L.append("  (F12 → Network) で SINR 等を返す URL を特定し、手動で設定してください。")
    return "\n".join(L)
