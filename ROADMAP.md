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
  傳送端要當 PTP 主時鐘並以 buffered 模式送出，pyatv 沒有這部分；唯一的開源傳送端實作是 owntone（C、GPL）。
- 本專案不販售，GPL 元件可用；若引入 owntone 的做法，整個專案改為 GPL-3.0（見 README 授權）。

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

## 4. 其他待辦

- MMCSS「Pro Audio」排程給擷取與傳送執行緒（穩定度）。
- Google Cast / DLNA 實機驗證（需裝置）。
- ALAC 編碼（可減 20–30% 空中時間，對 Wi-Fi 斷音有小幫助；需編碼器）。
