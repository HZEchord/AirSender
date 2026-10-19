# 變更記錄

格式依循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本號依循語意化版本。

## [未發布]

## [0.3.0] - 2026-10-19

### 新增
- 控制面板（左鍵點系統匣圖示）：狀態、接收端下拉選單、音量滑桿、接收端緩衝 / 抖動緩衝滑桿、
  音質與擷取來源設定、自動啟動選項；放開滑桿即套用
- 延遲預設新增 150 ms（實測總延遲約 0.44 s）與實驗性 100 ms（HomePod 會斷音）

### 變更
- 宣告 Per-Monitor DPI 感知：系統匣選單、對話框與面板在高 DPI 螢幕上不再模糊
- 右鍵選單簡化：狀態拆成多行、首項為「開啟控制面板…」
- 擷取執行緒自行初始化 COM，避免其他執行緒先建立 PyAudio 時出現 -9999 錯誤
- 量測工具不再把臨時設定寫回使用者設定檔

## [0.2.0] - 2026-10-19

### 新增
- Google Cast、DLNA / UPnP MediaRenderer、Snapcast 三種輸出端；共用的 HTTP 即時 WAV 伺服器（`/stream.wav`、`/stream.pcm`）
- AirPlay 接收端配對（RAOP / AirPlay，PIN 兩種流程）、密碼設定與憑證儲存
- 系統匣：重新連線、音量 ±5、配對與密碼、新增 / 移除 Snapcast 伺服器、接收端依種類分組
- `THIRD_PARTY_NOTICES.md`、`CHANGELOG.md`、GitHub Actions 建置流程

### 變更
- WASAPI 擷取改為 callback 模式：stop() 在 0.25 s 內返回並確實釋放裝置
- 兩段式重取樣（固定比率 VHQ → 44.1 kHz 可變速率微調），擷取端 CPU 由 4.8% 降至 3.2% 單核心
- dither / 轉換全部就地運算；預設輸出裝置探測 5 s → 10 s

### 修正
- 起始時 pyatv 補送造成的抖動緩衝抽乾（預填改在計時開始前完成）
- Windows 沒有程式播音時 loopback 不產生資料：資料恢復後重新預填並暫停漂移控制器

## [0.1.0] - 2026-10-19

### 新增
- WASAPI loopback 擷取 → soxr 重取樣 → TPDF dither → pyatv 以未壓縮 PCM 經 AirPlay 2 送到 HomePod
- 接收端緩衝可調（250 ms 起）、PI 漂移補償、Windows 1 ms 計時器
- 系統匣、命令列、麥克風端對端延遲量測工具、PyInstaller + Inno Setup 安裝程式
