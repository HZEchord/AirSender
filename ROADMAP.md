# 路線圖

記錄尚未實作、等待裝置才能驗證的目標與已知資訊。目前只有一顆 HomePod mini 可測，以下項目先記著。

## 1. HomePod 立體聲對（下一個目標）

**目標**：在家庭 App 配成一對的兩顆 HomePod，AirSender 以一台裝置的方式串流，左右聲道正確、兩顆同步。

**已知**

- 配成一對後，對外只廣播一個 AirPlay 2 端點，由主 HomePod 轉發給另一顆並在兩者之間自行同步；傳送端不需要分別連線。
- 立體聲對只走 AirPlay 2；Roon 社群回報過配對後的 HomePod 會被辨識成「Apple TV」並要求密碼、或韌體更新後行為改變，顯示相容性會隨 audioOS 版本變動
  （[Roon：Stereo Pair Support](https://community.roonlabs.com/t/apple-homepod-stereo-pair-support/48700?page=3)、
  [Roon：AirPlay 2 sees Stereo paired HomePods as Apple TV](https://community.roonlabs.com/t/airplay-2-sees-stereo-paired-homepods-as-apple-tv/276301)）。
- pyatv 的 AirPlay 2 走 realtime 模式（NTP 式時間同步）。立體聲對理論上能直接收，但未驗證；若主 HomePod 要求 buffered 模式或 PTP，就需要補協定。
- WinAirCast 特別標榜「立體聲對的微秒級 PTP 時鐘」，暗示傳送端可能需要 PTP 才能穩定。

**測試計畫（有第二顆 HomePod 時）**

1. 家庭 App 配成一對 → AirSender 重新掃描，確認只出現一台裝置、identifier 與 model。
2. 串流：確認左右聲道（用只在左聲道 / 只在右聲道的測試音）、是否斷音、`重傳` 計數。
3. 同步量測：`tools/measure_sync.py`（待寫）：左聲道播掃頻 A、右聲道播掃頻 B（不同頻率範圍），麥克風放兩顆中間，
   量兩者抵達時間差；目標 < 1 ms。
4. 若失敗：先試 `latencyMin` 回 250 ms、再試 buffered 模式（需在 pyatv 之外實作 SETUP type 0x67 與 TCP 音訊）。

## 2. 多房間毫秒級精準同步（最終目標）

**目標**：兩顆**未配對**的 HomePod 放在同一空間同時播放，偏差 < 1 ms（聽不出梳狀濾波與定位飄移）。

**現況與差距**

- 現在的架構一次只串流到一台裝置。「一源多連線」版（一個擷取源餵多條 AirPlay 連線，各自 NTP 同步）能做到跨房間夠用的數毫秒偏差，但同一空間不夠。
- 毫秒級以下需要 **PTP（IEEE 1588）**：AirPlay 2 的接收端在 buffered 模式用 PTP 對齊，shairport-sync 以 `nqptp` 實作接收端
  （[nqptp 手冊](https://man.archlinux.org/man/nqptp.8.en)：獨占 UDP 319 / 320、需 root）。
  **2026-10-21 更新：v0.11.0 已實作 buffered + PTP（`airsender/ptp.py`、`airsender/buffered.py`），單顆 HomePod mini 驗證可用。**
  實際抓包與實作確認：Mac → HomePod 時是 **HomePod 當主時鐘**，傳送端只是從屬（priority1 250 輸給 248），
  SETRATEANCHORTIME 的 networkTimeTimelineID 填 HomePod 的 grandmasterIdentity。多房間時兩顆 HomePod 會各自當主時鐘，
  傳送端得分別跟隨兩個時鐘、或自己當 grandmaster（airplay2-rs 回報 HomePod 忽略它的 Sync，待驗證）。參考的開源傳送端：
  - [airplay2-rs](https://github.com/lmcgartland/airplay2-rs)（Rust、GPL-3.0、2026-02 標 Alpha）：自稱 PTP 計時與多房間群組串流可用，
    HomePod mini 實測過；PTP 以較低優先權當 master 讓 HomePod 可搶；ALAC / AAC 編碼；只提 macOS / Linux（319 / 320 要 sudo）。
  - [streamsemble](https://github.com/aprilmaccydee/streamsemble)（C# / .NET 8、AGPL-3.0）：一個 hub 時鐘獨占 UDP 319 / 320 當 grandmaster，
    buffered 走 AAC over TCP（type 103），兩台 shairport-sync 量到約 1.8 ms 偏差；HomePod 未測。
  - owntone（C、GPL）：AirPlay 2 傳送端走 realtime，未見 PTP。
- Windows 上綁定 UDP 319 / 320 不需要管理員權限（沒有特權埠的概念），只要沒有別的程式占用；時間戳用 QPC（軟體時間戳，
  數十 µs 抖動），HomePod 端會濾波，毫秒級同步應該夠。單顆 HomePod 就能做並驗證 buffered + PTP（24-bit 由麥克風驗純度、
  延遲由現有量測工具驗），不必等第二顆。
- 本專案不販售，GPL / AGPL 元件可用；若引入上述專案的程式碼，整個專案改為對應授權（見 README 授權）。

**實作順序**

1. 一源多連線（AirPlay 多台同時播、各自音量）：約一天，立刻能用在跨房間。
2. 量測工具 `measure_sync.py`（同上）。
3. buffered 模式 + 傳送端 PTP：研究 owntone 的 `airplay.c` / `rtp_common.c`，先做單台 buffered，再做 PTP 主時鐘。

## 3. 沒有更多裝置時能做的測試

- **軟體接收端當第二台**：樹莓派或 WSL2 / Docker 跑 shairport-sync（AirPlay 2 需 nqptp），就能測「一源多連線」與同步量測；
  輸出接到任何有喇叭的 DAC 讓麥克風聽得到。
- **HomePod 內建麥克風**：不可行。HomePod 對外只透過 HomeKit 公開溫濕度感測器（區網上可見 `HomePodSensor…._hap._tcp`），
  沒有任何音訊特徵值；Apple 不開放第三方存取 HomePod 麥克風（[9to5Mac](https://9to5mac.com/2019/10/21/smart-speakers-can-eavesdrop/)）。
  對講功能與 Siri 的錄音都不可程式化取得。所有聲學量測仍以電腦麥克風為準。

## 3.5 24-bit / 48 kHz 無損到 HomePod：不是不可能，但要 buffered 模式 + PTP

2026-10-21 直接向 HomePod mini（audioOS 980.77.2）要 `/info`，`supportedFormats` 的位元遮罩說明了格式限制來自哪裡：

| 串流類型 | 遮罩位元 | 格式（依社群逆向的碼表） |
| --- | --- | --- |
| `audioStream`（realtime，type 96，AirSender 目前用的） | 11, 18, 22, 24 | PCM 44.1/16、ALAC 44.1/16、AAC-LC 44.1、AAC-ELD 44.1 |
| `lowLatencyAudioStream` | 25 | AAC-ELD 48 kHz（有損，FaceTime 類用途） |
| `bufferStream`（buffered，type 103） | 19, 21, 22, 23, 33–63 | **ALAC 44.1/24、ALAC 48/24**、AAC-LC 44.1/48、以及碼表未收錄的更高位元 |

- realtime 模式實測（直接送測試音、麥克風驗證音高與純度）：44.1 與 48 kHz 的 16-bit PCM 正確播放
  （遮罩雖未列 48 kHz 仍可用，已做成「AirPlay 取樣率」選項）；24-bit 以 BE24 / LE24 / S32BE / S32LE 四種打包送出
  都被當 16-bit 解讀成雜訊；88.2 / 96 kHz 半速播放。結論：realtime = 16-bit 固定、取樣率上限 48 kHz。
- buffered（type 103）SETUP 在 timingProtocol=NTP 下回 400 Bad Request，證實需要 PTP。
- buffered 模式宣告支援 ALAC 24-bit（44.1 與 48 kHz）。實例：HomePod 自己抓 Apple Music 無損後分享給另一顆 HomePod，
  就是以 AirPlay 24-bit/48 kHz 傳送（[Digital Trends](https://digitaltrends.com/?p=3278297)）。
- 代價：buffered 模式需要傳送端提供 PTP 時鐘、以 TCP 送 ALAC 幀、自行編碼 24-bit ALAC；pyatv 完全沒有這些。
  這與第 2 節的多房間 PTP 是同一套工程，做一次可同時得到「24/48 無損」與「毫秒級多房間同步」。
- **已實作（v0.11.0）**：buffered 48 kHz / 24-bit ALAC（未壓縮框架）實測可播，麥克風驗證音高與純度正常。
  HomePod 的 SETUP 回應：`audioBufferSize` 6144000、`arrivalToRenderLatencyMs` 4、`controlPort` 0（buffered 不用控制埠）、
  `skipRecord` true；`supportedAudioFormatsExtended.bufferStream` 還列了第 71–75 位元（碼表未收錄，可能是更高格式）。
- buffered 封包的 SSRC 是格式碼：0x15000000 = ALAC 48 kHz / 24-bit（shairport-sync 從 Apple 傳送端學到的值）。
  44.1 kHz / 24-bit 的碼未知，所以 buffered 固定 48 kHz。
- 延遲實測（提前量 → 端對端）：60 → 111 ms、75 → 127、100 → 149、350 → 400；50 與 25 ms 斷音。比 realtime 地板 140 ms 低。
- 音樂模式預設 buffered「標準」（提前 200 ms ≈ 250 ms）；遊戲 / 影片模式 0.11.1 起也預設 buffered「較低」（提前 75 ms ≈ 130 ms），控制面板可切回 realtime。

碼表來源：[airplay2-rs AIRPLAY_2_SPEC.md](https://github.com/lmcgartland/airplay2-rs/blob/master/AIRPLAY_2_SPEC.md)、
[SteeBono airplayreceiver wiki](https://github.com/SteeBono/airplayreceiver/wiki/AirPlay2-Protocol)；位元 33 以上無公開對照。

## 4. 其他待辦

- MMCSS「Pro Audio」排程給擷取與傳送執行緒（穩定度）。
- buffered：真正的 ALAC 壓縮（省 20–30% 頻寬）；44.1 kHz / 24-bit 的 SSRC 碼；長時間 Wi-Fi 抖動下 60–75 ms 提前量的穩定度。
- 多房間：一個擷取源餵兩條 buffered 連線，各自跟隨該 HomePod 的 PTP 時鐘並以相同的擷取時間對齊錨點。
- Google Cast / DLNA 實機驗證（需裝置）。
- ALAC 編碼（可減 20–30% 空中時間，對 Wi-Fi 斷音有小幫助；需編碼器）。
