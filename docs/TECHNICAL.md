# AirSender 技術手冊

這份文件說明 AirSender 用了哪些技術與原理、每個功能是怎麼做到的、哪些數字是實測來的，
以及目前已知的技術債。對象是想讀程式碼或想改它的人。使用說明在 README.md，未完成的目標在 ROADMAP.md。

目錄

1. [整體架構](#1-整體架構)
2. [音訊擷取：WASAPI loopback](#2-音訊擷取wasapi-loopback)
3. [重取樣、位元深度與 dither](#3-重取樣位元深度與-dither)
4. [AirPlay 2 的兩種傳輸方式](#4-airplay-2-的兩種傳輸方式)
5. [PTP 時鐘同步](#5-ptp-時鐘同步)
6. [延遲是怎麼組成的](#6-延遲是怎麼組成的)
7. [時鐘漂移補償](#7-時鐘漂移補償)
8. [補靜音：來源沒聲音時發生什麼事](#8-補靜音來源沒聲音時發生什麼事)
9. [房間校正與低音增強](#9-房間校正與低音增強)
10. [延遲校正（麥克風）](#10-延遲校正麥克風)
11. [其他接收端：Cast、DLNA、Snapcast、HTTP](#11-其他接收端castdlnasnapcasthttp)
12. [使用者介面與系統整合](#12-使用者介面與系統整合)
13. [打包、安裝、更新](#13-打包安裝更新)
14. [測試方法](#14-測試方法)
15. [安全性](#15-安全性)
16. [技術債與可維護性](#16-技術債與可維護性)

---

## 1. 整體架構

```
Windows 混音引擎 ──WASAPI loopback──▶ capture.py ──▶ ringbuffer.py（含時間標記）
                                      │  soxr 重取樣（固定比率 + 可變比率漂移段）
                                      │  房間校正 FIR（dsp.py / roomcal.py）
                                      │  縮放到 16 或 24-bit、TPDF dither
                                      ▼
                     live_source.py（pyatv AudioSource；抖動緩衝、DriftController）
                       ├─ realtime ──▶ pyatv RAOP / AirPlay 2（patches.py 補丁）──UDP──▶ 接收端
                       └─ buffered ──▶ buffered.py（ALAC 24-bit、TCP、SETRATEANCHORTIME）
                                       + ptp.py（PTP 從屬時鐘，UDP 319 / 320）──▶ HomePod
                     httpstream.py（PCM 廣播 + aiohttp WAV）──▶ sinks/（Cast、DLNA）、Snapcast TCP
streamer.py：掃描、連線、重連、狀態機（獨立 asyncio 執行緒）
tray.py + ui.py：pystray 系統匣、tkinter 快捷面板 / 控制面板（獨立 UI 執行緒）
config.py：設定檔（%APPDATA%\AirSender\config.json）、模式 / 傳輸 / 預設組的邏輯
```

執行緒：

| 執行緒 | 工作 |
| --- | --- |
| 主執行緒 | pystray 訊息迴圈（系統匣） |
| UI 執行緒 | tkinter 的 `mainloop`；其他執行緒以 `UiThread.call()` 投遞工作 |
| stream-loop | asyncio 事件迴圈：RTSP、封包推送、PTP、HTTP 伺服器 |
| WASAPI callback | PortAudio 的擷取回呼，做重取樣與 EQ 後推進環形緩衝 |
| 工作執行緒 | 校正、量測、靜音端點等會阻塞的操作 |

設計原則：音訊路徑上不配置記憶體（就地運算）、不持鎖等待；UI 與串流完全分離，面板只是每秒讀一次統計。

## 2. 音訊擷取：WASAPI loopback

- 用 PyAudioWPatch（PortAudio 的 WASAPI loopback 分支）打開「預設輸出裝置 [Loopback]」，以 callback 模式收 10 ms 區塊。
  拿到的是 Windows 混音引擎送往裝置的 float32 資料，格式等於該裝置在「聲音設定」裡的共享模式格式
  （例如 FiiO KA13 設 176.4 kHz 就拿到 176.4 kHz）。
- 擷取執行緒自行 `CoInitializeEx`（否則其他執行緒先建 PyAudio 時會出現 −9999 錯誤）。
- 預設輸出裝置每 10 秒檢查一次，換裝置時重開擷取。
- `stop()` 在 0.25 秒內返回並釋放裝置（callback 模式的好處）。
- 每個區塊推進環形緩衝時記下 `perf_counter_ns`，buffered 模式用這個時間標記做時間錨（見第 6 節）。

限制：loopback 拿到的是整台電腦的混音，沒有「只擷取某個程式」。Windows 10 2004 起有 process loopback API
（`ActivateAudioInterfaceAsync` + `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`），PortAudio 沒有包，
要自己以 ctypes 呼叫 COM，列在技術債。

## 3. 重取樣、位元深度與 dither

兩段式重取樣（libsoxr）：

1. 固定比率 VHQ：來源取樣率 → 目標取樣率（例如 176.4 k → 48 k）。固定比率讓 soxr 用最高品質的多相濾波器，SNR 約 150 dB。
2. 可變比率段：目標取樣率 → 目標取樣率 × (1 + adj)，adj 在 ±0.1% 內，由漂移控制器決定（第 7 節）。
   拆成兩段是因為可變比率模式品質較低，把它限制在極小的比率變化上，耗 CPU 也少（擷取端由 4.8% 降到 3.2% 單核心）。

位元深度：

- realtime 傳輸固定 16-bit（HomePod 的 realtime 解碼只收 16-bit，24-bit 各種打包都被當 16-bit 解成雜訊，實測）。
  float 樣本乘 32768 後 TPDF dither（兩個均勻分布相減，±1 LSB 三角分布）再取整。
  若區塊已經是 16-bit 整數值（來源 16-bit、音量 100%、沒有重取樣與 EQ）就不加 dither，樣本與來源完全相同，狀態列標 bit-perfect。
- buffered 傳輸 24-bit：乘 2^23，以 int32 容器承載，dither 在 24-bit 的 LSB。16-bit 來源會原樣放大 256 倍，仍是 bit-perfect。

Windows 混音器本身也會把所有程式的聲音重取樣到裝置格式，所以「DAC 設 176.4 kHz、音樂 44.1 kHz」會經過兩次轉換；
README 說明了怎麼把轉換減到最少。

## 4. AirPlay 2 的兩種傳輸方式

| | realtime（stream type 96） | buffered（stream type 103） |
| --- | --- | --- |
| 傳輸 | UDP，每封包 352 frames 的 raw PCM（ct = 1） | TCP，每封包 352 frames 的 ALAC 框架（ct = 2） |
| 時間基準 | NTP 式：接收端依封包節奏與 RTP 時間戳播放，pyatv 負責計時 | PTP：接收端依共用時鐘在「指定的絕對時刻」播放 |
| 緩衝 | 本機抖動緩衝 + HomePod 最小緩衝（latencyMin）+ 接收端緩衝（RTP 提前量） | 只有「提前量」：傳送端把每個封包提早送到，HomePod 自己排程 |
| 加密 | ChaCha20-Poly1305，nonce = 序號，AAD = 時間戳 ‖ SSRC | 同一套，nonce = 64-bit 封包計數器，AAD = rtpTime ‖ SSRC |
| 位元深度 | 16 | 24 |
| 實作 | pyatv 內建，`patches.py` 補丁 | `buffered.py`、`ptp.py`、`alac.py`，不經 pyatv 的串流程式 |

### 4.1 realtime 與 pyatv 補丁

pyatv 0.18 的 RAOP 實作本來是放檔案用的。`patches.py` 以 monkeypatch 改了四處：

1. `open_source`：接受我們的 `LiveAudioSource`（即時來源），不走 miniaudio 解碼。
2. `StreamContext.reset`：接收端延遲（RTP 時間戳提前量）原本寫死 1.5 秒，改為可設定。
3. `RtspSession.setup`：AirPlay 2 SETUP 的 `latencyMin` / `latencyMax` 原本寫死 250 ms / 2 s；
   實測 `latencyMin` 就是 HomePod 的最小緩衝，降到 25 ms 總延遲從約 440 ms 掉到約 140 ms。同一處也覆寫 `sr` 與 `audioFormat` 旗標（44.1 / 48 kHz）。
4. `ControlClient._retransmit_lost_packets`：統計接收端要求重傳的封包數（Wi-Fi 抖動指標）。

pyatv 開始串流時會先補送一段封包，會把抖動緩衝抽乾；`LiveAudioSource` 在計時開始前預填、第一次讀取時裁掉握手期間累積的舊資料。

### 4.2 buffered 的 RTSP 流程

```
GET /info → pair-verify（HAP，連線之後全程加密）
SETUP { timingProtocol: "PTP", timingPeerInfo: {Addresses: [本機 IP], ID, SupportsClockPortMatchingOverride: true}, … }
   ← eventPort、timingPeerInfo（含 HomePod 的 ClockID）、skipRecord
TCP 連到 eventPort（HomePod 會透過它送 updateInfo 之類的請求，要回 200）
RECORD（Range: npt=0-、RTP-Info: seq=0;rtptime=0）
SETPEERS /peer-list-changed [本機 IP, HomePod IP]
SETUP { streams: [{ type: 103, ct: 2, audioFormat: 0x200000（ALAC 48/24）, sr: 48000, spf: 352, shk: 32 bytes 隨機,
                     asc: ALACSpecificConfig(24 bytes), latencyMin/Max, controlPort, streamConnectionID }] }
   ← dataPort（TCP）、audioBufferSize 6144000、arrivalToRenderLatencyMs 4、controlPort 0
TCP 連到 dataPort → 等 PTP 鎖定 → primer 封包（空負載，帶第一個 rtpTime）
SETRATEANCHORTIME { rate: 1, rtpTime, networkTimeSecs, networkTimeFrac（2^64 分數）, networkTimeTimelineID（HomePod 的時鐘 ID）}
持續推送；每 2 秒 POST /feedback；結束 TEARDOWN
```

封包格式（接收端以 shairport-sync 的解析與抓包為準）：

```
u16 BE 總長（含這 2 bytes）
u32 BE 0x80000000 | 23-bit 序號        ← 不是標準 RTP 的 marker/PT 位置，Apple 把序號塞進這裡
u32 BE rtpTime（每封包 +352）
u32 BE SSRC = 0x15000000               ← 其實是格式碼：ALAC 48 kHz / 24-bit 立體聲
ChaCha20-Poly1305 密文 ‖ 16-byte tag ‖ 8-byte nonce（封包計數器 LE）
```

ALAC 框架用「escape」模式（`alac.py`）：樣本原樣寫入、不做預測與 Rice 編碼。解碼端照常接受（FFmpeg 與 Apple 的解碼器都處理這個旗標），
所以不必實作壓縮器就能無損送 24-bit，代價是頻寬等同 PCM（約 2.3 Mbit/s）。框架結構：3 bits 元件型別（CPE = 1）、4 bits tag、
12 bits 保留、1 bit partial、2 bits shift、1 bit escape = 1、352 × (L 24 bits, R 24 bits)、3 bits END = 7、補零到位元組。

44.1 kHz / 24-bit 的 SSRC 格式碼沒有公開資料，所以 buffered 固定 48 kHz。

## 5. PTP 時鐘同步

buffered 模式的時間基準是 IEEE 1588 風格的 PTP（Apple 的 profile：unicast、UDP 319 事件埠 / 320 一般埠、
來源埠必須等於目的埠、domain 0、transportSpecific = 1）。

誰是主時鐘：**HomePod**。Mac → HomePod 的抓包、以及我們的實作都是 HomePod 以 priority1 248 當 grandmaster，
傳送端以 priority1 250 參與 BMCA 讓它勝出（`ptp.py`）。傳送端做的事：

1. SETUP 後 HomePod 會主動送 Announce + Signaling 過來；我們回 Announce（priority1 250）與 Mac 風格的 Signaling
   （802.1AS Message Interval Request TLV + 兩個 Apple 自訂 TLV，值取自抓包），並每秒重送一次。
2. 收 Sync（記下到達時刻 t2）與 Follow_Up（t1 = preciseOriginTimestamp + correctionField）。
3. 每個 Follow_Up 後送 Delay_Req（t3），收 Delay_Resp（t4）。
4. 偏移估計：m = t2 − t1、s = t4 − t3，在 8 秒視窗內取各自的最小值（「最幸運的封包」最接近真實路徑延遲），
   offset = (min m − min s) / 2、path = (min m + min s) / 2。實測路徑延遲約 0.25 ms、視窗內抖動 0.3–1.3 ms。
5. `master_now = local_now − offset`；SETRATEANCHORTIME 的 networkTime 就以此換算，timelineID 填 HomePod 的 grandmasterIdentity。

本機時鐘以 `perf_counter_ns` 為基礎（單調、不會因系統對時而倒退），clockIdentity 由 MAC 位址做成 EUI-64。
Windows 綁 319 / 320 不需要管理員權限，但同一台電腦只能有一個程式綁，所以安裝版與開發用的量測工具不能同時跑。

沒有做的：自己當 grandmaster（多房間時可能需要；airplay2-rs 回報 HomePod 忽略傳送端的 Sync，待驗證）、頻率差估計（視窗最小值法在數十 ppm 的時鐘差下誤差 < 1 ms，夠用）。

## 6. 延遲是怎麼組成的

realtime：總延遲 ≈ latencyMin（HomePod 最小緩衝）+ 接收端緩衝（RTP 提前量）+ 本機抖動緩衝 + 約 15 ms。
HomePod mini 實測地板約 140 ms（25 / 100 / 20）；接收端緩衝低於 100 ms 不再更低，反而重播。

buffered：總延遲 ≈ 提前量 + 約 45–50 ms。提前量是「封包的播放時刻 − 封包的擷取時刻」；
約 45 ms 的固定開銷 = Windows 混音到 loopback 的延遲（約 10–20 ms）+ 擷取區塊 10 ms + HomePod 的 arrivalToRender 4 ms + 聲音傳播與麥克風。

時間錨怎麼定（這是 0.12.0 修正過的地方）：第一個封包的 rtpTime 在「主時鐘的（該封包擷取時刻）+ 提前量」播放。
以擷取時刻而不是「現在」為基準，是因為擷取區塊每 10 ms 一塊、有時成串到達，用「現在」錨定會把當下的隨機相位（0–30 ms）
烙進整條時間軸，每次串流的總延遲就不一樣（修正前同設定量到 127 與 147 ms，修正後三次 123 / 122 / 117 ms）。

實測（HomePod mini，麥克風端對端，`tools/measure_latency.py`）：

| 提前量 | 總延遲 |
| --- | --- |
| 60 ms | 111 ms（5/5 一致） |
| 75 ms | 117–127 ms |
| 100 ms | 149 ms |
| 200 ms | 252 ms |
| 350 ms | 400 ms |
| 50 / 25 ms | 斷音，量不到 |

HomePod 需要封包在播放時刻前約 60–70 ms 抵達，這是接收端的限制。

## 7. 時鐘漂移補償

問題：電腦音效裝置的時鐘與接收端的時鐘有數十到數百 ppm 的差異（本機實測 FiiO 約 +700 ppm 相對於 pyatv 的節奏）。
不補償的話，緩衝每分鐘累積或流失數十毫秒，最後跳音或延遲飄走。

做法（`DriftController`，live_source.py）：PI 控制器輸出重取樣比率的微調量 adj（±0.1% 上限，約 1.7 音分，聽不出音高差）。

- realtime：量測值 = 本機抖動緩衝的資料量（ms），目標 = 設定的抖動緩衝。緩衝變多 → adj > 0 → 可變比率段少產生樣本 → 緩衝回落。
- buffered：量測值 = 每個封包的「播放時刻 − 擷取時刻」，目標 = 提前量。這個量不受封包到達節奏影響，只反映兩邊時鐘的差。

參數：kp = 3e-5、ki = 2.5e-7（自然頻率約 0.016 rad/s，時間常數約一分鐘，接近臨界阻尼）；量測值先過 0.5 秒一階低通，
濾掉封包節奏造成的 ±5 ms 抖動。狀態列的「漂移」就是 adj（ppm）；偏離目標時會暫時變大，約一分鐘內收斂。
realtime 模式另有「固定比率 + 裁切」選項：不微調，緩衝超過上限一次裁掉（會有可聽的接縫，但重取樣品質最高）。

## 8. 補靜音：來源沒聲音時發生什麼事

Windows 沒有任何程式在播音時，WASAPI loopback 不會送資料（引擎大約在最後一個串流關閉幾秒後停止）。
兩種傳輸的處理不同：

- realtime：pyatv 以固定節奏呼叫 `readframes()`，緩衝空了就回傳靜音（underrun 計數 +1），
  資料恢復後先等抖動緩衝重新累積到目標量再開始送（re-prime），期間送靜音；漂移控制器暫停。
- buffered：推送迴圈發現「下一個封包的播放時刻」逼近到只剩「提前量 − 2 個封包」，就補靜音封包直到提前量回到目標，
  時間軸保持連續、HomePod 不會因為沒資料而停播或重新同步。補靜音期間漂移控制器不更新
  （此時提前量由補償邏輯決定，不代表時鐘差）。資料恢復時，真實資料接在補的靜音後面，播放時刻 − 擷取時刻仍約等於提前量。

**靜音久了會不會超出上限或崩潰？** 不會，所有會增長的量都有界：

| 量 | 行為 |
| --- | --- |
| RTP 時間戳 | 32-bit，每封包 +352，約 24.9 小時繞回一次；所有運算都用 `& 0xFFFFFFFF` 做模運算 |
| 23-bit 序號 | 同樣模運算 |
| 加密 nonce 計數器 | 64-bit，實際上不會用完 |
| 補靜音封包數、總封包數 | Python 整數，只是統計 |
| PTP 樣本 | 8 秒視窗的 deque，舊樣本自動丟掉；待配對的 Sync / Delay_Req 表最多 16 筆 |
| 環形緩衝 | 固定容量，溢位丟最舊的資料並計數 |
| HomePod 端的緩衝 | 我們永遠只送到「提前量」那麼遠，不會持續堆積 |

實測連續補靜音數十秒、資料恢復、再靜音的循環正常；長時間（數小時）的靜音沒有專門測過，但沒有無界的狀態。

### 8.1 無聲待命、被接管時靜默等待、睡眠喚醒（0.14.0）

`Streamer._run_supervised()` 把每一次串流包成子任務，每 2 秒檢查一次：

- **無聲待命**：擷取端在每個區塊記下「最近一次非靜音（峰值 > −80 dBFS）」的時刻。串流中若連續無聲超過 `idle_standby_minutes`
  （預設 10 分鐘，0 = 關閉），就取消串流、狀態改為「待命（無聲）」，改用一個只看有沒有聲音的輕量擷取監看
  （loopback 在無聲時不會送資料，所以幾乎不耗 CPU）；偵測到聲音就重新連線，約 2–3 秒恢復，這段聲音會漏掉。
- **被接管時靜默等待**：串流被接收端正常結束（最常見是手機 AirPlay 接管）後，先以 GET /info 的 `statusFlags`
  判斷接收端是否仍被其他來源使用；是的話進入「待命（接收端被占用）」，每 10 秒查一次，釋放後才連回，不會把 HomePod 搶回來。
  判定位元 `BUSY_FLAG_MASK` 需要比對「閒置」與「被別的來源使用」兩種狀態下的 flags 才能確定，未填入前此功能退回原本的立即重連。
- **睡眠喚醒**：監看迴圈兩次醒來相隔超過 30 秒，表示系統睡眠過（Wi-Fi 與接收端連線都已失效），直接取消舊串流、2 秒後重連，
  不等 TCP / RTSP 逾時。不需要視窗訊息迴圈，所以不依賴 `WM_POWERBROADCAST`。

這三個行為的測試在 `tests/test_streamer_supervise.py`，以假的串流函式取代真實接收端。

## 9. 房間校正與低音增強

- 量測：串流中播放 3 次 6 秒的對數掃頻（Farina 法），電腦麥克風在聆聽位置錄音，用反向濾波器反摺積得到脈衝響應。
  訊噪比以峰值對峰值前 200 ms 的雜訊估計，實測 55 dB。
- 分析：頻率相依視窗（低頻 500 ms 含房間駐波、高頻 8 ms 只看直達聲），1/6 八度平滑；支援 1–3 個位置平均與 REW 格式麥克風校正檔。
- 修正：預設只修 40–500 Hz（Schroeder 頻率以下、房間主導的區域；高頻受麥克風指向與桌面反射影響太大，實機校正證明不該修），
  最多 +6 / −12 dB，乘上強度，加上 0–9 dB 的低音低架。
- 濾波器：由目標幅度響應以倒頻譜法做最小相位 FIR（4096 taps），FFT 重疊相加即時處理，每 10 ms 區塊約 0.18 ms，零額外區塊延遲；
  依濾波器增益自動預先衰減避免削波。
- 校正設定檔存在 `%APPDATA%\AirSender\room_correction.json`，開啟 EQ 後就不是 bit-perfect，狀態列會反映。

HomePod 自己的麥克風拿不到（Apple 只透過 HomeKit 公開溫濕度感測器），所以一切聲學量測都靠電腦麥克風。

## 10. 延遲校正（麥克風）

控制面板「校正延遲…」與 `tools/measure_latency.py` 共用 `latencycal.py`：

1. 串流穩定後，由電腦喇叭輸出 5 聲 50 ms 的線性掃頻（1–6 kHz，振幅 0.3），每聲間隔 3 秒，記下送出時刻（含輸出裝置延遲）。
2. 麥克風同時錄音，記下第一個樣本的時刻（含輸入裝置延遲）。
3. 錄音先 FFT 帶通到掃頻頻帶，再與掃頻做匹配濾波（處理增益約 24 dB），在每次送出後的 2.9 秒內找峰值；
   訊噪比 = 峰值 / 區間中位數，低於 10 倍視為雜音。
4. 100 ms 以內的峰值視為電腦自己喇叭的直達聲，之後的才是接收端；取彼此相差 15 ms 內的最大一群的中位數。

結果以傳輸方式為鍵存進設定（`latency_measured`），面板的估計值改為「公式 + （實測 − 當時公式）」，換預設組會跟著換算。
量到的數字可直接填進播放器的音訊延遲補償。

## 11. 其他接收端：Cast、DLNA、Snapcast、HTTP

同一個擷取源可以餵 `PcmBroadcaster`，由 aiohttp 提供 `/stream.wav`（WAV 標頭 + 無限 PCM）與 `/stream.pcm`：

- Google Cast（pychromecast）：叫裝置播 `http://<本機>:8765/stream.wav`。實機未驗證（測試時裝置待機）。
- DLNA / UPnP（async_upnp_client）：SetAVTransportURI 同一個 URL。未測。
- Snapcast：直接以 TCP 推 PCM 到 snapserver 的 tcp 來源（port 4953）。本機驗證過。
- 任何播放器也可以直接開那個 URL。

這些接收端自己決定緩衝，延遲設定對它們無效，房間校正仍會套用。

## 12. 使用者介面與系統整合

- 系統匣：pystray（Win32 Shell_NotifyIcon）。右鍵選單精簡；左鍵彈出快捷面板。
- 快捷面板：tkinter `Toplevel`、`overrideredirect` 無邊框、置頂，開啟時以滑鼠位置為錨點固定，之後每秒重新擺放（內容高度可能變）。
  收起條件：焦點離開本程式且下拉沒展開且滑鼠不在面板上（0.13.0 起），或按 Esc。
  面板上沒有「?」說明，說明集中在控制面板。
- 控制面板：所有設定；「?」圖示的懸浮提示文字預先換行；狀態列固定寬度等寬欄位，數字變長不推動版面。
- 三處設定（右鍵選單、快捷面板、控制面板）共用 `Config`，每秒同步，拖曳滑桿時不覆寫。
- DPI：程序宣告 Per-Monitor DPI 感知，字體不模糊；所有像素以 `ui.px()` 換算。
- 靜音電腦輸出（0.13.0）：pycaw 設定輸出端點的靜音旗標。硬體靜音的裝置（USB DAC、HDMI、藍牙）loopback 不受影響；
  軟體音量的裝置靜音後 loopback 也無聲。只還原自己設的靜音，使用者原本就靜音的端點不動；停止串流與結束程式時還原。
- 防火牆：AirPlay 2 要接收端反向連回電腦（事件通道、PTP），Windows 公用網路設定檔會擋新程式；
  安裝程式以 netsh 加規則，程式內也有 UAC 按鈕補加。
- 自動啟動：登入時啟動以 HKCU Run 登錄機碼實作。

## 13. 打包、安裝、更新

- PyInstaller onedir（`AirSender.spec`，含 LGPL 元件的通知檔）；Inno Setup 安裝程式（系統管理員，加防火牆規則，
  升級時先清空 `_internal` 不留舊版殘檔，解除安裝可選擇刪除設定與記錄）。
- GitHub Actions：push 建置，tag 發布 Release 並附安裝程式；Dependabot 每週檢查相依。
- 更新檢查：查 GitHub Releases API，下載後驗 SHA-256、靜默安裝、重新啟動；預設關閉（天數 0）。

## 14. 測試方法

- 單元測試（`tests/`）：DSP（掃頻 / 反摺積 / 最小相位 FIR）、格式選擇、模式與預設組邏輯、ALAC 框架（以獨立的位元讀取器解回）、
  PTP 訊息打包 / 解析、偏移估計、buffered 封包框架（以接收端規則解密）、環形緩衝時間標記、延遲量測的訊號處理（合成錄音）。
- 實機：`tools/measure_latency.py --mode game|music --latency N` 量端對端延遲；`tools/room_calibrate.py --verify` 校正前後比對；
  麥克風驗音高 / 純度（48 kHz 與 24-bit 的判定都是這樣來的）；`--list`、`--headless` 命令列串流。
- 串流迴圈本身沒有自動化測試（需要真實接收端）；版本發布前以實機跑一次兩種傳輸與模式切換。

## 15. 安全性

攻擊面與對策（0.14.0 依白帽審查修正；審查報告的完整內容見 CHANGELOG）：

| 面向 | 風險 | 現在的做法 |
| --- | --- | --- |
| HTTP 串流伺服器（Cast / DLNA 用） | 原本綁 0.0.0.0、無驗證、啟動後不關，區網任何主機都能聽電腦音訊 | 只在需要時啟動、結束即關；綁定接收端對應的本機介面；路徑含隨機 token；只接受目前接收端 IP；最多 8 客戶端 |
| 防火牆規則 | 放行整個程式、所有設定檔、任意來源 | 加 `remoteip=localsubnet`；程式內以 .cmd 執行 netsh，避免 cmd 展開 `%VAR%` |
| 自動更新 | digest 缺少時不驗、URL 不檢查、安裝程式未簽章卻以管理員靜默執行、暫存路徑可預測（TOCTOU） | 無 SHA-256 即拒絕；只接受 GitHub HTTPS；token 只送 api.github.com；新建私有暫存夾、200 MB 上限、檔名白名單；啟動前再驗雜湊。**仍缺程式碼簽章**，更新鏈的信任根是 GitHub 帳號與 CI |
| PTP（UDP 319 / 320） | 來源 IP 可偽造，注入假 Follow_Up 可把時鐘偏移拉歪（buffered 斷音） | 綁本機 IP；只接受 clockIdentity = 已宣告主時鐘；版本 / domain / 長度檢查；偏離中位數 1 秒即丟；回應限速 |
| 憑證 | `config.json` 明文存 HAP 憑證與密碼 | DPAPI 使用者範圍加密（`secretstore.py`），舊明文讀入後下次儲存即加密 |
| 記錄 | pyatv DEBUG 會印 RTSP 內容（含串流金鑰、配對資料） | `DEBUG` 只開 AirSender；`TRACE` 才開 pyatv |
| 區網資料 | mDNS / SSDP 名稱可含控制字元；SSDP LOCATION 可指向任意 URL | 名稱只留可列印字元、限 64 字；LOCATION 必須指向回應者本身 |
| 連線韌性 | buffered 連線無逾時、接收端停止讀取會卡死 | 連線 10 秒、送出 5 秒逾時，逾時即重連 |
| 供應鏈 | Actions 以可變 tag 引用、工作層級的寫入權限、相依未鎖定 | Actions 釘 commit SHA；建置工作唯讀、發布工作限 tag；`requirements.lock` 雜湊鎖定；Inno Setup 版本固定 |
| 隨機數 | `random` 用於串流金鑰 / PIN | 改用 `secrets` |

仍然成立的假設：區網內的裝置是可信的（AirPlay 本身的信任模型）；同一 Windows 帳號下的其他程式不是攻擊者（DPAPI 使用者範圍擋不住它們）。

## 16. 技術債與可維護性

誠實列出，按影響排序（0.14.0 已處理第 4、9、11 項的部分）：

1. **pyatv 的 monkeypatch**（`patches.py`）：改了四個私有函式，pyatv 版本一動就可能壞；`pyproject.toml` 釘死 0.18.0。
   長期解法是把 realtime 也改成自己的實作（buffered 已經是了，RTSP / HAP 部分仍共用 pyatv，那些是公開 API，較穩）。
2. **兩條傳輸路徑**：realtime 走 pyatv 的推送節奏，buffered 走自己的迴圈；統計、狀態、音量各有一份對應。
   功能新增時容易只改一邊。可考慮把 realtime 也搬到 `buffered.py` 同一個推送模型（UDP + NTP 計時封包），之後再統一。
3. **ui.py 太大**（約 1300 行，tkinter）：快捷面板、控制面板、狀態列、校正流程全在一檔；沒有 UI 測試，只靠截圖腳本目視。
   拆成 panel/quick/controls 模組、把「設定 → 顯示」的對應表資料化會好改很多。
4. **設定遷移是硬編的**：`Config.load()` 裡逐條判斷舊預設值；沒有版本號。若之後常改預設，應加 `config_version` 與遷移表。
5. **格式碼靠逆向**：SSRC 0x15000000、audioFormat 旗標、Apple PTP TLV 的值都來自抓包與社群整理；沒有官方文件，
   audioOS 更新可能改行為。ROADMAP §3.5 記了來源與驗證方法。
6. **PTP 只做從屬**：多房間、立體聲對、或 HomePod 不願當主時鐘的情況沒覆蓋。
7. **主控台編碼**：記錄檔是 UTF-8，但 Windows 主控台（cp950）印中文會亂碼，開發時用記錄檔看。
8. **安裝包 44 MB**：numpy、aiohttp、pychromecast 等全帶；Cast / DLNA 未驗證卻占體積，可改為可選。
9. **靜音端點的相容性**：軟體音量裝置會連擷取一起靜音，目前只在說明裡提醒，沒有自動偵測。
10. **UDP 319 / 320 獨占**：開發工具與安裝版互斥；錯誤訊息已說明，但沒有自動退讓（例如改走 realtime）。
11. **延遲量測精度**：單次量測散佈約 ±10 ms（擷取區塊 10 ms），用中位數；想更準要縮短區塊或多量幾次。
12. **未驗證的功能**：Google Cast、DLNA 沒有實機；buffered 60–75 ms 提前量在長時間 Wi-Fi 抖動下的穩定度沒有長測。
