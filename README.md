# AirSender

把 Windows 正在播放的所有聲音，以**未壓縮 PCM** 透過 AirPlay 送到 **HomePod / HomePod mini**（也支援 Apple TV 與其他支援 AirPlay 音訊的接收端）。以低延遲、不壓縮、最高音質為目標。

- 擷取：WASAPI loopback，直接抓 Windows 混音輸出（float32，裝置原生取樣率）
- 重取樣：libsoxr（固定比率 VHQ，或可變速率模式做時鐘漂移補償），TPDF dither 轉 16-bit
- 傳輸：AirPlay 2（HAP 加密通道）+ RAOP，音訊格式為 **44.1 kHz / 16-bit / 立體聲 raw PCM**，完全不經有損壓縮
- 介面：系統匣小程式（右下角圖示），另附命令列工具
- 安裝：Windows 安裝程式（Inno Setup），或從原始碼執行

## 關於延遲與音質的誠實說明

| 項目 | 數值 | 說明 |
| --- | --- | --- |
| 音訊格式 | 44.1 kHz / 16-bit / 2 ch PCM | AirPlay 音訊協定的上限。來源若是 48k / 96k / 176.4k 會以 soxr 重取樣（SNR > 120 dB），24-bit 以上會加 TPDF dither 轉 16-bit |
| 本機延遲 | 約 50–60 ms | WASAPI 10 ms 區塊 + 40 ms 抖動緩衝（可調 20–150 ms） |
| 接收端緩衝 | 250 ms 起（預設 500 ms） | AirPlay 接收端收到封包後等多久才播，是整體延遲的主要來源。HomePod mini 實測 250 ms 可穩定播放。設太低會斷音 |
| 總延遲（實測） | **約 520–550 ms**（接收端緩衝 250 ms 時） | 用電腦麥克風實測 HomePod mini 發聲的端對端延遲（見下方「量測延遲」）。HomePod 自身約再加 200 ms 內部處理，這部分無法從傳送端縮短。Apple 自家 macOS → HomePod 約 2 秒 |

協定限制：AirPlay 音訊串流只接受 44.1 kHz / 16-bit。這是 Apple 的接收端限制，不是本程式的限制。

HomePod 的麥克風只供它自己（Siri、內建房間自適應）使用，Apple 沒有開放任何介面讓外部裝置取得，所以無法用它來做測試或分析；房間回音補償由 HomePod 內部自動進行。

## 安裝

### 方法一：安裝程式（建議）

1. 執行 `dist\installer\AirSender-Setup-x.y.z.exe`（自行建置請見下方）
2. 安裝程式會要求系統管理員權限，用來加入 Windows 防火牆入站規則。**這一步很重要**：AirPlay 2 的 SETUP 需要 HomePod 反向連回電腦，被防火牆攔下時症狀是「no response to SETUP」逾時而連不上，在「公用」網路設定檔下尤其如此
3. 安裝時可勾選「登入 Windows 時自動啟動」
4. 若選擇了非管理員安裝，之後可在系統匣選單選「允許通過 Windows 防火牆」補上規則（會跳 UAC）

### 方法二：從原始碼執行

需要 Python 3.10 以上（開發環境為 3.13）。

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m airsender
```

### 自行建置安裝程式

需要 [Inno Setup 6](https://jrsoftware.org/isinfo.php)（`winget install -e --id JRSoftware.InnoSetup`）。

```powershell
.\build.ps1
```

產生：

- `dist\AirSender\AirSender.exe`：系統匣程式（免安裝也可直接執行整個資料夾）
- `dist\AirSender\AirSenderCLI.exe`：命令列版本
- `dist\installer\AirSender-Setup-x.y.z.exe`：安裝程式

## 使用

### 系統匣

啟動後右下角出現喇叭圖示（灰色 = 閒置，藍色 = 串流中）。右鍵選單：

- **接收端**：列出網路上的 AirPlay 裝置，點選即開始串流；「重新掃描」更新清單
- **開始 / 停止串流**：左鍵雙擊圖示也可切換
- **接收端音量**：直接控制 HomePod 音量（或選「不控制」）
- **延遲**：接收端緩衝 250 / 500 / 1000 / 1500 / 2000 ms，以及本機抖動緩衝 20–150 ms
- **音質**：
  - 時鐘漂移補償：「平滑」用可變速率重取樣微調 ±0.1%（約 1.7 音分，聽不出來），永不跳音；「固定比率 VHQ + 裁切」用最高品質固定比率，緩衝累積過多時一次裁掉（每幾分鐘一次極短跳音）
  - 固定比率模式的 soxr 品質
  - TPDF dither 開關
- **擷取來源**：跟隨 Windows 預設輸出裝置（切換裝置會自動跟上），或指定某個輸出裝置
- **啟動後自動連線到上次裝置**、**登入 Windows 時自動啟動**
- **開啟記錄資料夾**：`%LOCALAPPDATA%\AirSender\logs`

設定檔在 `%APPDATA%\AirSender\config.json`。

### 命令列

```bash
AirSenderCLI.exe --list
```

```bash
AirSenderCLI.exe --device 客廳 --headless --latency 250
```

其他參數：`--jitter 20`、`--volume 60`、`--drift trim`、`--quality VHQ`、`--no-dither`、`-v`。
從原始碼執行時以 `python -m airsender ...` 取代 `AirSenderCLI.exe`。

## 量測延遲（用電腦麥克風）

```bash
.venv\Scripts\python tools\measure_latency.py --latency 250 --volume 15 --repeats 6
```

工具會以低音量（HomePod 15%）送出 6 個 50 ms 的 1–6 kHz 掃頻音，同時用電腦麥克風錄音，
對錄音做帶通濾波與匹配濾波（交叉相關），找出掃頻音從 HomePod 傳回的時間。抗干擾設計：

- 帶通 1–6 kHz，濾掉人聲、空調、交通等雜音
- 匹配濾波處理增益約 24 dB，並顯示每次的訊噪比（實測 600–1200 倍）
- 多次量測只採信彼此在 ±15 ms 內一致的結果，離群值自動排除

實測結果（HomePod mini，Wi-Fi，接收端緩衝 250 ms，本機抖動緩衝 40 ms）：中位數 548 ms，6 次介於 542–549 ms。
若電腦接有喇叭，會先看到一個較早的峰值（電腦喇叭），HomePod 的峰值在 100 ms 之後。

## 行為細節

- **Windows 沒有任何程式在播音時**，WASAPI loopback 不會送出資料，AirSender 會送靜音維持連線。播放恢復時會先重新累積到抖動緩衝量再開始送（期間仍是靜音），確保延遲與穩定度與剛開始串流時一致。
- **時鐘漂移**：電腦音效裝置與傳送節奏的時鐘通常有數十到數百 ppm 的差異（本機實測約 +700 ppm）。「平滑」模式以 PI 控制器微調重取樣比率（上限 ±0.1%），讓本機緩衝穩定在目標值，不會跳音。
- **預設輸出裝置切換**時（例如插入耳機），擷取會在 5 秒內自動跟上。

## 建議的網路設定（對延遲影響最大）

- 電腦走**有線網路**，少一段無線跳躍
- HomePod 連 **5 GHz**（HomePod mini 只支援 Wi-Fi 4 / 802.11n，但 5 GHz 的 n 已足夠：PCM 只需 1.4 Mbps）
- 電腦與 HomePod 在同一個子網路、同一台 AP 下
- 若 HomePod 在「家庭」App 設了需要密碼，目前不支援（pyatv 的 RAOP 密碼支援僅限舊式接收端）

## 疑難排解

| 現象 | 原因 / 做法 |
| --- | --- |
| 找不到接收端 | 確認同一子網路；防火牆允許 mDNS（UDP 5353）；公司網路常封鎖 |
| 「no response to SETUP」或「pair-pin-start」逾時 | Windows 防火牆攔下 HomePod 的反向連線。系統匣選單 →「允許通過 Windows 防火牆」，或重新以管理員身分執行安裝程式 |
| 連上但沒聲音 | 檢查 Windows 是否真的有在播放、擷取來源是否正確；HomePod 音量是否為 0 |
| 斷斷續續 | 把「延遲」調高一級；電腦改用有線；關閉 2.4 GHz |
| 一開始有 30–50 ms 靜音 | 正常，pyatv 起始時會補送封包；之後穩定 |
| 聲音與畫面對不上 | AirPlay 本質延遲，可用播放器（VLC、mpv）把音訊提前對應的毫秒數 |

## 架構

```
Windows 混音 ──WASAPI loopback──▶ capture.py（soxr 重取樣 + dither）
        ──▶ ringbuffer ──▶ live_source.py（抖動緩衝 + 漂移 PI 控制）
        ──▶ pyatv RAOP/AirPlay 2（ChaCha20 加密 raw PCM）──▶ HomePod
```

- `airsender/capture.py`：WASAPI loopback 執行緒、重取樣、dither
- `airsender/live_source.py`：pyatv `AudioSource` 實作、抖動緩衝、`DriftController`
- `airsender/patches.py`：對 pyatv 的兩個補丁（接受即時來源；接收端延遲可設定，pyatv 原本寫死 1.5 秒）
- `airsender/streamer.py`：掃描、連線、串流、自動重連（獨立 asyncio 執行緒）
- `airsender/tray.py`：pystray 系統匣
- `airsender/__main__.py`：命令列入口
- `tools/measure_latency.py`：端對端延遲量測（麥克風）
- `AirSender.spec`、`installer/AirSender.iss`、`build.ps1`：打包與安裝程式

## 致謝

- [pyatv](https://pyatv.dev/)：AirPlay / RAOP / HAP 協定實作
- [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch)：WASAPI loopback
- [python-soxr](https://github.com/dofuuz/python-soxr)：libsoxr 重取樣
- [pystray](https://github.com/moses-palmer/pystray)：系統匣
