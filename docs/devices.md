# 機器ごとの接続方法・取得可否

各端末から「何を・どうやって」取得するかをまとめます。RF 項目は機種・ファーム
により取得可否が変わるため、取れない項目は `None` として扱い、可能な範囲で
TLE ベースの予測(向き・接続衛星)で補完します。

---

## 1. Starlink Mini —「電波用 WebGUI が無い」への回答

Starlink には RF 専用 WebGUI はありませんが、**Dish 本体(Dishy)が
`192.168.100.1:9200` で認証不要の gRPC を公開**しており、ここから電波側に近い
メトリクスが取得できます。これが Starlink 監視のソリューションです。

### 接続
- 本ソフトの動作 PC から Dish へ IP 到達性が必要（ルータ配下なら 192.168.100.1
  へのルーティング/スタティックルート、または Dish 直下に測定 PC を置く）。
- `grpcurl`（<https://github.com/fullstorydev/grpcurl>）を OS に導入。

```bash
grpcurl -plaintext -d '{"get_status":{}}' 192.168.100.1:9200 \
        SpaceX.API.Device.Device/Handle
```

### 取得できる主な項目（→ RFSample へのマッピング）
| RFSample | Starlink フィールド | 備考 |
|---|---|---|
| `azimuth_deg` | `alignmentStats.boresightAzimuthDeg` | アンテナの向き（方位） |
| `elevation_deg` | `alignmentStats.boresightElevationDeg` | アンテナの向き（仰角） |
| `tilt_deg` | `alignmentStats.tiltAngleDeg` | |
| `snr_above_noise` | `isSnrAboveNoiseFloor` | 新ファームは数値 SNR 非公開。真偽で代替 |
| `sinr_db` | `snr` | 旧ファームのみ数値。0 のことが多い |
| `down_bps`/`up_bps` | `downlinkThroughputBps`/`uplinkThroughputBps` | 端末実測 |
| `latency_ms` | `popPingLatencyMs` | PoP までの遅延 |
| `drop_rate` | `popPingDropRate` | |
| `obstruction_pct` | `obstructionStats.fractionObstructed`×100 | 障害物率 |

### 制約
- **使用周波数は非公開**（`tx_freq_mhz`/`rx_freq_mhz` は取得不可）。
- SNR 数値は多くの機体で非公開。ノイズフロア以上か(真偽)で品質を判断します。

---

## 2. Kymeta（OneWeb 運用）—「IP 指定で WebGUI から記録」

Kymeta u8 / Osprey は端末 IP の **HTTPS WebGUI** を持ち、Status ページに SINR・
スキャン角(Θ=broadside からの角度、`仰角 = 90° − Θ` 等)などを表示します。RF を
見られる WebGUI はこれのみ、という要件に対応します。

### 接続（`config.yaml`）
```yaml
- name: "kymeta-oneweb-1"
  kind: "kymeta"
  enabled: true
  base_url: "https://<端末IP>"
  verify_tls: false           # 自己署名証明書
  username: "admin"
  password: "***"
  endpoints:
    status_json: "/api/status"   # 内部 JSON API があれば優先
    status_html: "/status"       # 無ければ HTML をスクレイプ
  field_map:                     # 実機に合わせて調整（下記手順）
    sinr_db: "rf.sinr"
    azimuth_deg: "pointing.azimuth"
    elevation_deg: "pointing.elevation"
    tx_freq_mhz: "rf.tx_frequency_mhz"
    rx_freq_mhz: "rf.rx_frequency_mhz"
```

### field_map の調べ方
1. WebGUI を Firefox で開き、開発者ツール → Network を確認。
2. Status ページが裏で JSON API を叩いていれば、その URL を `status_json` に、
   JSON のキー階層（`a.b.c`）を `field_map` の値に設定（推奨・堅牢）。
3. JSON が無く HTML 直書きなら `status_html` を設定し、`field_map` の値を
   **正規表現**にする（例: `sinr_db: "SINR[^0-9]*([0-9.]+)"` — 最初のキャプチャ
   グループを数値として採用）。

> 機種・ファームで DOM/API が異なるため、マッピングは実機合わせが前提です。
> 本体コードは変更不要で、`config.yaml` の調整だけで対応できます。

---

## 3. OneWeb Intellian（OW11 等）—「監視 WebGUI が無い」への回答

Intellian の OneWeb 端末は公開 API を提供せず、標準の監視手段は LUI(ローカル
Web UI)/モバイルアプリに限られます。本ソフトは 2 経路で取得を試みます。

### (a) SNMP（機体・構成が対応していれば最も確実）
```yaml
- name: "intellian-oneweb-1"
  kind: "intellian"
  enabled: true
  snmp:
    enabled: true
    host: "192.168.100.1"
    community: "public"
    oids:                       # 実機 MIB に合わせる
      sinr_db: "1.3.6.1.4.1.<vendor>.<...>"
      elevation_deg: "1.3.6.1.4.1.<vendor>.<...>"
```

### (b) LUI スクレイプ（Kymeta と同方式）
`base_url` + `endpoints`/`field_map` で JSON/HTML から取得。

### 補完策
LUI が SINR/周波数を出さない機体では、**ハンドオーバー予測モジュール**が
「自局から見て最も条件の良い OneWeb 可視衛星と、その仰角・方位」を推定し、
アンテナが向くべき方向の目安・接続衛星の推定として利用できます。

---

## 補足: 使用周波数について
Starlink は非公開、Kymeta/Intellian も LUI が出す場合のみ取得可能です。ビーカ/
帯域を厳密に知るには衛星事業者側の運用情報が必要で、地上端末からは一般に
限定的にしか見えない点にご留意ください。
