# AirSender

> **免費公開、不販售。** 本專案以 MIT 授權開源，供任何人免費使用與修改；作者不販售本軟體。
> 詳見下方「授權」。

把 Windows 正在播放的所有聲音，以**未壓縮 PCM** 透過 AirPlay 送到 **HomePod / HomePod mini**（也支援 Apple TV 與其他支援 AirPlay 音訊的接收端），並可輸出到 **Google Cast、DLNA / UPnP、Snapcast** 接收端。以低延遲、不壓縮、最高音質為目標。

| 接收端種類 | 路徑 | 延遲 | 驗證狀態 |
| --- | --- | --- | --- |
| AirPlay（HomePod、Apple TV、Sonos / Yamaha / Denon 等 AirPlay 2 機種、shairport-sync） | pyatv 推送 raw PCM | **約 0.5 秒**（實測 HomePod mini 520–550 ms） | 已實測 HomePod mini |
| Google Cast（Chromecast、Nest 音響、Cast 電視） | 接收端拉 HTTP WAV | 2–4 秒（接收端預緩衝） | 已實作；本機只有一台待機中的 Cast 機上盒，無法完整驗證 |
| DLNA / UPnP MediaRenderer | 接收端拉 HTTP WAV | 1–5 秒 | 已實作；本機無 DLNA 裝置可驗證 |
| Snapcast（snapserver tcp 來源） | TCP 推 raw PCM | 由 snapserver buffer 決定（300–1000 ms） | 已以本機假伺服器驗證資料速率 |
| 任何播放器（VLC、foobar2000、手機 App…） | 開記錄檔裡印出的 `http://<電腦IP>:8765/<token>/stream.wav`（需設定 `http_allow_any_client`） | 播放器緩衝決定 | 已驗證 |

- 擷取：WASAPI loopback，直接抓 Windows 混音輸出（float32，裝置原生取樣率）
- 重取樣：libsoxr（固定比率 VHQ，或可變速率模式做時鐘漂移補償），TPDF dither 轉 16-bit
- 傳輸：AirPlay 2 流程（HAP 配對、ChaCha20-Poly1305 加密音訊封包）+ RAOP 控制，音訊格式為 **44.1 kHz / 16-bit / 立體聲 raw PCM**，完全不經有損壓縮
- 介面：系統匣小程式（右下角圖示），另附命令列工具
- 安裝：Windows 安裝程式（Inno Setup），或從原始碼執行

## 關於延遲與音質的誠實說明

| 項目 | 數值 | 說明 |
| --- | --- | --- |
| 音訊格式 | realtime：44.1 或 48 kHz / 16-bit PCM；**buffered：48 kHz / 24-bit ALAC（無損、未壓縮框架）** | realtime 的 16-bit 是 HomePod 的解碼限制；buffered 走 Apple 自己多房間用的路徑，24-bit 實測可播。來源取樣率不同時以 soxr 重取樣（SNR > 120 dB） |
| 本機延遲 | 約 30–55 ms | WASAPI 10 ms 區塊 + 抖動緩衝 20–40 ms（可調 10–150 ms） |
| HomePod 最小緩衝（latencyMin） | 25–250 ms（預設 50） | AirPlay 2 SETUP 參數，pyatv 原本寫死 250 ms。這才是先前「HomePod 內部 250 ms」的真面目，可降到 25 ms |
| 接收端緩衝 | 100 ms 起（預設 150） | RTP 時間戳的提前量。HomePod mini 低於 100 ms 不再降低總延遲且會重播 |
| 總延遲（實測，realtime） | **約 140 ms**（最低組 25 / 100 / 20）、約 220 ms（「較低」25 / 150 / 30） | 用電腦麥克風實測 HomePod mini 發聲的端對端延遲（見下方「量測延遲」），公式：latencyMin + 接收端緩衝 + 抖動緩衝 + 約 15 ms。Apple 自家 macOS → HomePod 約 2 秒 |
| 總延遲（實測，buffered） | **提前 60 ms → 111 ms**、75 → 127（遊戲 / 影片模式預設「較低」）、100 → 149、200 → 約 250（音樂模式預設「標準」）、350 → 400 ms | 公式：提前量 + 約 50 ms。50 ms 以下 HomePod 跟不上會斷音 |

協定限制：realtime 串流只接受 16-bit（44.1 或 48 kHz）；buffered 串流可收 24-bit，但需要傳送端參與 PTP 時鐘同步（本程式已實作）。

HomePod 的麥克風只供它自己（Siri、內建房間自適應）使用，Apple 沒有開放任何介面讓外部裝置取得，所以無法用它來做測試或分析；房間回音補償由 HomePod 內部自動進行。

## 安裝

### 方法一：安裝程式（建議）

1. 執行 `dist\installer\AirSender-Setup-x.y.z.exe`（自行建置請見下方）
2. 安裝程式會要求系統管理員權限，用來加入 Windows 防火牆入站規則。**這一步很重要**：AirPlay 2 的 SETUP 需要 HomePod 反向連回電腦，被防火牆攔下時症狀是「no response to SETUP」逾時而連不上，在「公用」網路設定檔下尤其如此
3. 安裝時可勾選「登入 Windows 時自動啟動」
4. 若選擇了非管理員安裝，之後可在系統匣選單選「允許通過 Windows 防火牆」補上規則（會跳 UAC）

### 升級

直接執行新版安裝程式即可，不必先解除安裝：會安裝到同一目錄、覆蓋並清掉舊版殘檔、保留設定與校正檔、
關閉執行中的程式後重新建立防火牆規則；「應用程式與功能」只會有一筆。

### 解除安裝

「設定 → 應用程式」移除 AirSender。解除安裝程式會：結束執行中的程式、刪除安裝目錄、開始功能表與桌面捷徑、
登入自動啟動的登錄值、Windows 防火牆規則，最後詢問是否一併刪除設定檔（`%APPDATA%\AirSender`）
與記錄（`%LOCALAPPDATA%\AirSender`）。選「是」即不留任何檔案；選「否」保留設定供日後重裝沿用。

靜默解除安裝並清除使用者資料：

```bash
"C:\Program Files\AirSender\unins000.exe" /VERYSILENT /REMOVEUSERDATA=1
```

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

### 快捷面板、控制面板與系統匣

啟動後右下角出現喇叭圖示（灰色 = 閒置，藍色 = 串流中）。

- **左鍵**點圖示：快捷面板。停在右下角，點了不會關（點別處或 Esc 才收起）：狀態、開始 / 停止、重新連線、
  接收端、音量滑桿、模式切換（遊戲 / 影片、音樂）、「控制面板…」。
- **控制面板**：完整設定。接收端緩衝與本機抖動緩衝滑桿（放開即套用並重新連線）、時鐘漂移補償、重取樣品質、
  dither、擷取來源、自動啟動、配對與密碼、防火牆、記錄資料夾。
- **右鍵**選單只留：狀態、快捷面板、控制面板、開始 / 停止、接收端、結束。

三處共用同一份設定，改了任何一處其他兩處會立即跟著變。右鍵選單細節：

- **接收端**：列出網路上的 AirPlay 裝置，點選即開始串流；「重新掃描」更新清單
- **開始 / 停止串流**：左鍵雙擊圖示也可切換
- **接收端音量**：直接控制 HomePod 音量（或選「不控制」）
- **無聲待命**：連續無聲 N 分鐘（預設 10）就斷線待命，有聲音自動連回；接收端被手機接管時靜默等待、釋放後才連回；睡眠喚醒後立即重連
- **串流時靜音電腦輸出**（控制面板最下方的選項）：電腦這邊不出聲、HomePod 照常；USB DAC / HDMI / 藍牙等硬體靜音的裝置適用，內建類比輸出可能連擷取一起靜音
- **延遲**：模式（遊戲 / 影片 = 盡量壓低延遲，預設 buffered「較低」約 130 ms；音樂 = 大緩衝求穩，預設 buffered「標準」約 250 ms），
  每個模式記住自己的傳輸方式（realtime / buffered）與預設組（最低 / 較低 / 低 / 標準 / 高 / 最穩）；realtime 可再個別調整 HomePod 最小緩衝、接收端緩衝、本機抖動緩衝
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
AirSenderCLI.exe --device 客廳 --headless --latency 100 --latency-min 25 --jitter 20
```

其他參數：`--jitter 20`、`--volume 60`、`--drift trim`、`--quality VHQ`、`--no-dither`、`-v`。
從原始碼執行時以 `python -m airsender ...` 取代 `AirSenderCLI.exe`。

## 量測延遲（用電腦麥克風）

最簡單的方式是控制面板「延遲」區的 **「校正延遲…」**：串流中播 5 聲短促掃頻音、電腦麥克風錄音，
算出真正的端對端延遲並記住；之後面板的「≈」估計值會以實測修正。量到的數字可直接填進播放器的音訊延遲補償
（mpv、PotPlayer、VLC），畫面就能完全對上。命令列版本如下：

```bash
.venv\Scripts\python tools\measure_latency.py --latency 250 --volume 15 --repeats 6
```

工具會以低音量（HomePod 15%）送出 6 個 50 ms 的 1–6 kHz 掃頻音，同時用電腦麥克風錄音，
對錄音做帶通濾波與匹配濾波（交叉相關），找出掃頻音從 HomePod 傳回的時間。抗干擾設計：

- 帶通 1–6 kHz，濾掉人聲、空調、交通等雜音
- 匹配濾波處理增益約 24 dB，並顯示每次的訊噪比（實測 600–1200 倍）
- 多次量測只採信彼此在 ±15 ms 內一致的結果，離群值自動排除

實測結果（HomePod mini，Wi-Fi，`--latency-min` 為 HomePod 最小緩衝）：

| latencyMin | 接收端緩衝 | 抖動緩衝 | 端對端延遲（5 次中位數） |
| --- | --- | --- | --- |
| 250（pyatv 原值） | 250 | 40 | 548 ms |
| 250 | 150 | 20 | 437 ms |
| 100 | 150 | 20 | 283 ms |
| 50 | 150 | 20 | 235 ms |
| 25 | 150 | 20 | 191 ms |
| 25 | 100 | 20 | **137 ms** |
| 25 | 75 | 20 | 138 ms（不再更低） |
| 25 | 50 | 20 | 142 ms，出現重播峰值 |
若電腦接有喇叭，會先看到一個較早的峰值（電腦喇叭），HomePod 的峰值在 100 ms 之後。

## 兩種傳輸方式：realtime 與 buffered

| | realtime | buffered（兩個模式的預設） |
| --- | --- | --- |
| AirPlay 2 串流類型 | type 96，UDP | type 103，TCP |
| 格式 | PCM 16-bit，44.1 / 48 kHz | ALAC 24-bit，48 kHz（未壓縮框架，仍是無損） |
| 時間基準 | NTP 式，接收端依封包節奏播放 | PTP：HomePod 當主時鐘，傳送端告訴它「rtpTime X 在時刻 T 播放」 |
| 緩衝在哪 | 本機抖動緩衝 + HomePod 最小緩衝 + 接收端緩衝 | 只有「提前量」一個參數，Wi-Fi 抖動由 HomePod 的緩衝吸收 |
| 實測總延遲 | 地板約 140 ms，常用 220 ms | 提前 60 ms → 111 ms，100 → 149，200 → 250 |
| 成熟度 | pyatv 內建路徑，用最久 | 0.11.0 新增，單顆 HomePod mini 驗證 |

控制面板「延遲」區可為每個模式分別選擇傳輸方式；兩種各有自己的預設組。buffered 的 PTP 部分：
傳送端在 UDP 319 / 320 上以 unicast 與 HomePod 交換 Announce / Sync / Follow_Up / Delay_Req，以 priority1 250 參與 BMCA
讓 HomePod（248）勝出並當主時鐘，用最小延遲的樣本估計時鐘差（實測路徑延遲約 0.25 ms、抖動 0.3–1.3 ms）。
Windows 綁這兩個埠不需要管理員權限，但同一台電腦不能同時跑別的 PTP 程式。

## 無損、bit-perfect 與格式轉換的真相

AirPlay realtime 串流在 HomePod mini 上實測可收 **44.1 或 48 kHz，位元深度固定 16-bit**（24-bit 各種打包都被當 16-bit 解讀，
88.2 / 96 kHz 會半速播放）；buffered 串流則是 **48 kHz / 24-bit**（24-bit 來源不需 dither，16-bit 來源原樣放大 256 倍，仍是 bit-perfect）。
AirSender 在這些限制內做到「不經有損壓縮」；realtime 的「AirPlay 取樣率」預設自動：
擷取裝置是 48 / 96 / 192 kHz 時用 48 kHz，否則 44.1 kHz。來源格式與串流格式不同時就必須轉換：

| 情況 | AirSender 的處理 | 影響 |
| --- | --- | --- |
| Windows 輸出格式 = 44.1 kHz、來源 16-bit、音量 100%、未啟用 EQ | 樣本原樣通過（**bit-perfect**，狀態列會標示） | 無 |
| Windows 輸出格式 = 44.1 kHz、來源 24-bit 或音量非 100% | 只做 24 → 16-bit 的 TPDF dither | 底噪 −96 dBFS，遠低於房間噪音與 HomePod 自身底噪 |
| Windows 輸出格式 ≠ 串流取樣率（例如 DAC 176.4 kHz → 44.1 kHz，或 96 kHz → 48 kHz） | soxr VHQ 重取樣，再 dither | 22–24 kHz 以上內容移除（無可聽內容）；重取樣 SNR 約 150 dB |

要注意的是：**Windows 混音器本身**也會把所有來源重取樣到裝置的輸出格式。DAC 設在 176.4 kHz 時，
44.1 kHz 的音樂先被 Windows 升頻再被 AirSender 降回，等於兩次轉換。想把轉換減到最少：

1. 聲音設定 → 輸出裝置 → 內容 → 進階，把預設格式改為 **44100 Hz**（位元深度選 24 或 16 皆可）。
2. 此時 44.1 kHz 的來源（CD、Spotify、Apple Music 無損多為 44.1）就是 bit-perfect；48 kHz 的來源（YouTube、遊戲）
   由 Windows 重取樣一次。
3. Windows 音量維持 100%，用接收端音量控制大小聲；關閉輸出裝置的「增強」效果。

房間校正與低音增強是刻意的訊號處理，啟用後就不是 bit-perfect，這是取捨而非缺陷。

## 非 AirPlay 接收端

- **Google Cast / DLNA**：系統匣「接收端」會列出掃到的裝置；選擇後 AirSender 啟動 HTTP 伺服器（預設 port 8765，可在設定檔改 `http_port`）並要接收端來拉 `stream.wav`。這條路徑無損但延遲由接收端預緩衝決定，系統匣會標示。延遲選單對這些裝置無效。
- **Snapcast**：「接收端 → 新增 Snapcast 伺服器…」輸入 snapserver 位址。snapserver.conf 需有：

  ```
  [stream]
  source = tcp://0.0.0.0:4953?name=AirSender&mode=server&sampleformat=44100:16:2
  ```

  命令列可直接 `AirSenderCLI.exe --headless --snapcast 192.168.1.10`。
- **任何播放器**：0.14.0 起 HTTP 串流只在選了 Cast / DLNA 裝置時啟動、串流結束就關閉，URL 含每次啟動隨機的 token，
  而且只接受目前選擇的接收端 IP。想讓區網裡任何播放器拉流，把設定檔的 `http_allow_any_client` 設為 true，
  並從記錄檔取得含 token 的 URL（`/<token>/stream.wav` 或 `/<token>/stream.pcm`）。
- 防火牆：Cast / DLNA 的探索依賴 mDNS / SSDP **多播回應**（入站 UDP），播放時接收端還要連入 TCP 8765；
  兩者都需要安裝程式加入的防火牆規則（放行整個程式，但只限本機子網 `remoteip=localsubnet`）。沒有規則時，打包版會掃不到 Cast / DLNA 裝置，
  但 AirPlay 不受影響（pyatv 的 mDNS 要求單播回應）。

## 房間校正與低音增強

HomePod 的運算音訊（房間感測、低音管理）只對 Apple 自家來源完整生效；AirSender 提供自己的一套：

1. 控制面板「房間校正與低音」→「開始校正…」。把電腦麥克風放在平常聆聽的位置（桌面、頭部高度），保持安靜。
2. 程式在串流中播放 3 次約 6 秒的對數掃頻（Farina 掃頻，REW / Trueplay 同款方法），用麥克風錄音後反摺積得到脈衝響應。
   低頻用 500 ms 長視窗把房間駐波算進去（桌面震不震的關鍵），高頻用 8 ms 短視窗只看直達聲，避免把反射修進 EQ。
3. 1/6 八度平滑後存成校正檔，之後串流時由最小相位 FIR（4096 taps）即時套用：預設只修正 40–500 Hz 的房間主導區（高頻受麥克風指向與位置影響太大，設定檔 `correction_f_high` 可放寬）、
   最多 +6 dB、−12 dB，並自動預先衰減避免 16-bit 削波（所以啟用後整體音量會降一些，用接收端音量補回）。
4. 「低音增強」是獨立的低架（100 Hz 以下 0–9 dB），不校正也能用；「校正強度」可把量測修正打折。
5. 可量 1–3 個位置平均；若有 REW 格式的麥克風校正檔，在設定檔 `mic_calibration_file` 指定。

限制：USB 麥克風通常在 50 Hz 以下不準，所以 40 Hz 以下不修正；HomePod mini 本身的低頻下限約 60 Hz，
給它過多 40–60 Hz 增益只會吃掉動態餘裕。

## 安全性設計（0.14.0 起）

- HAP 配對憑證與密碼以 Windows DPAPI（使用者範圍）加密後才寫進 `config.json`；換電腦或換帳號時重新配對即可。
- 自動更新只接受 GitHub 網域的 HTTPS 位址，Release 沒附 SHA-256 就拒絕安裝，啟動安裝程式前再驗一次雜湊；下載上限 200 MB。
- HTTP 串流伺服器：只在需要時啟動、路徑含隨機 token、只接受目前接收端的 IP、最多 8 個客戶端。
- PTP：只接受已宣告主時鐘（clockIdentity 相符）的計時訊息，偏離中位數 1 秒以上的樣本丟棄，回應限速。
- 防火牆規則限本機子網；`log_level` 為 `DEBUG` 時 pyatv 仍維持 INFO（協定內容含金鑰），要看協定細節請用 `TRACE`。
- 區網來的裝置名稱只留可列印字元；CI 的 Actions 釘在 commit、相依套件以雜湊鎖定、發布權限只給 tag 觸發的工作。
- 尚未做到：安裝程式沒有程式碼簽章（需要憑證），所以更新仍依賴 GitHub 帳號與 CI 的安全。

## 更新與維護

- **使用者端**：自動更新檢查**預設關閉**；在控制面板「更新」區把天數設為非 0 即可啟用，或按「立即檢查」。
  有新版時會通知；按「更新」會下載安裝程式、以 GitHub 提供的 SHA-256 驗證、靜默安裝後自動重新啟動。
  更新檢查需要倉庫為公開；私有倉庫測試可設環境變數 `AIRSENDER_GITHUB_TOKEN`。
- **開發端**：Dependabot 每週一檢查 pyatv 等套件與 GitHub Actions 的新版並開 PR，CI 自動 lint、測試、打包。
  維護流程只剩：看 PR → 合併 → 打 `vX.Y.Z` 標籤 → CI 發布 → 使用者端自動收到。

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

完整的技術說明（原理、實測數字、技術債）在 [docs/TECHNICAL.md](docs/TECHNICAL.md)。

```
Windows 混音 ──WASAPI loopback──▶ capture.py（soxr 重取樣 + dither，16 或 24-bit）
        ──▶ ringbuffer ──▶ live_source.py（抖動緩衝 + 漂移 PI 控制）
        ├─ realtime ──▶ pyatv RAOP/AirPlay 2（ChaCha20 加密 raw PCM，UDP）──▶ HomePod
        └─ buffered ──▶ buffered.py（ALAC 24-bit 框架、ChaCha20、TCP）+ ptp.py（PTP 從屬時鐘）──▶ HomePod
```

- `airsender/capture.py`：WASAPI loopback 執行緒、重取樣、dither
- `airsender/live_source.py`：pyatv `AudioSource` 實作、抖動緩衝、`DriftController`
- `airsender/patches.py`：對 pyatv 的兩個補丁（接受即時來源；接收端延遲可設定，pyatv 原本寫死 1.5 秒）
- `airsender/buffered.py`、`airsender/ptp.py`、`airsender/alac.py`：AirPlay 2 buffered 串流（type 103）、PTP 從屬時鐘、ALAC 未壓縮框架
- `airsender/streamer.py`：掃描、連線、串流、自動重連（獨立 asyncio 執行緒），依接收端種類分派
- `airsender/httpstream.py`：PCM 廣播器 + aiohttp WAV 伺服器（Cast / DLNA / 播放器共用）
- `airsender/sinks/`：`cast.py`（pychromecast）、`dlna.py`（async_upnp_client）、`snapcast.py`（TCP）
- `airsender/pairing.py`、`airsender/dialogs.py`：AirPlay 配對 / 密碼與輸入對話框
- `airsender/tray.py`：pystray 系統匣
- `airsender/__main__.py`：命令列入口
- `tools/measure_latency.py`：端對端延遲量測（麥克風）
- `AirSender.spec`、`installer/AirSender.iss`、`build.ps1`：打包與安裝程式

## 開發

提交前跑一次與 CI 相同的檢查（lint、格式、單元測試）：

```bash
.\check.ps1
```

個別指令：

```bash
.venv\Scripts\python -m ruff check airsender tools tests
```

```bash
.venv\Scripts\python -m ruff format airsender tools tests
```

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

測試不需要接收端：buffered 推送迴圈、串流監看（待命 / 喚醒 / 被占用）、PTP 訊息、ALAC 框架、設定遷移都用假物件測。
需要實機的驗證（延遲、音高、兩種傳輸）用 `tools/` 下的工具，見「量測延遲」。設定檔的預設值變更一律寫在 `config.py` 的
`MIGRATIONS`，不要散在載入程式裡。

效能參考（本機 i7 等級、176.4 kHz DAC、串流到 HomePod）：擷取 + 兩段式重取樣 + dither 約 3.2% 單核心，
pyatv 傳送路徑約 4–5% 單核心，合計約 7% 單核心（16 執行緒機器約 0.5% 整體）。

## 授權

- AirSender 的程式碼以 **MIT** 授權釋出（見 `LICENSE`），免費供任何人使用、修改與再散佈；作者不販售本軟體。
- 第三方元件授權見 `THIRD_PARTY_NOTICES.md`。其中 libsoxr、pystray、python-zeroconf 為 LGPL，以獨立 `.pyd` / `.dll`
  動態載入散佈，已符合 LGPL 的要求。
- 未來若為了降低延遲而引入 GPL 元件（例如 owntone 的 AirPlay 2 實作），整個專案的授權會改為 **GPL-3.0**，
  屆時會在此處與 CHANGELOG 明確標記。由於本專案不作商業販售，這不會造成使用上的限制。

## 致謝

- [pyatv](https://pyatv.dev/)：AirPlay / RAOP / HAP 協定實作
- [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch)：WASAPI loopback
- [python-soxr](https://github.com/dofuuz/python-soxr)：libsoxr 重取樣
- [pystray](https://github.com/moses-palmer/pystray)：系統匣
- [PyChromecast](https://github.com/home-assistant-libs/pychromecast)、[async_upnp_client](https://github.com/StevenLooman/async_upnp_client)、[aiohttp](https://github.com/aio-libs/aiohttp)：Cast / DLNA / HTTP 串流
- [swyh-rs](https://github.com/dheijl/swyh-rs)：HTTP 即時 WAV 串流設計的參考
