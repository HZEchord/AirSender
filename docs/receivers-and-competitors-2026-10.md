# AirSender 競品與接收端協定調查（2026-10）

> 所有版本／日期以本次查證為準；標「未驗證」者僅為二手資料或廠商宣稱。

## A. 類似應用（把 PC 音訊送到無線喇叭）

| 產品 | 平台 / 價格 | 協定 | 即時系統音訊 | 延遲宣稱 | 無損？ | 特色 / 狀態 |
|---|---|---|---|---|---|---|
| **TuneBlade** | Windows；免費版對硬體接收端每 10 分鐘中斷，完整版約 US$9.99 ([betanews](https://betanews.com/article/stream-audio-from-your-pc-to-any-airplay-device-using-tuneblade/)) | AirPlay 1 (RAOP) | 是 | 「Normal」約 2 s，「Real-time」較低 ([lifehacker.jp](https://www.lifehacker.jp/article/2301-you-can-use-apple-airplay-to-stream-from-a-windows-pc-t/)) | ALAC 無損 | 系統匣、多裝置同步。AlternativeTo 列為已停更，最後版 1.8.0.0 (2018-12) ([alternativeto](https://alternativeto.net/software/tuneblade/about))；有人回報 Win11 24H2 無法安裝 ([deskmodder](https://www.deskmodder.de/blog/?p=200952)) |
| **AirParrot 3** (Squirrels) | Windows / macOS；約 US$15.99–21.99，7 天試用 ([9to5mac](https://9to5mac.com/2020/05/26/airparrot-3-released-features/), [sspai](https://sspai.com/post/113002)) | AirPlay、Google Cast、螢幕鏡射 | 是（亦可送媒體檔） | 未公開數字 | 未明示 | 多接收端自動同步、各別音量、HomePod 支援 ([airsquirrels](https://www.airsquirrels.com/airparrot/features/win))；仍在販售 |
| **Airfoil** (Rogue Amoeba) | macOS 仍販售；**Windows 版 2019-12-23 停售** ([rogueamoeba](https://rogueamoeba.com/airfoil/windows/)) | AirPlay、Bluetooth、Chromecast (Mac) | 是 | 社群回報 PC→HomePod 約 2–3 s（未驗證） | ALAC | 可針對單一 App 擷取（Mac） |
| **Stream What You Hear (SWYH)** | Windows，免費/開源；原版 1.5.0 (2020-05) 已停更 ([alternativeto](https://alternativeto.net/software/stream-what-you-hear/about)) | DLNA/UPnP（內建 HTTP 伺服器） | 是 | 數秒（接收端緩衝決定） | WAV / MP3 | 原版有記憶體洩漏 |
| **swyh-rs** (SWYH 之 Rust 重寫) | Windows / Linux；MIT，v1.21.0，2026 仍活躍 ([github](https://github.com/dheijl/swyh-rs)) | UPnP/DLNA、OpenHome、SlimProto（Squeezelite）；SSDP 探索；HTTP :5901 | 是（WASAPI loopback / cpal） | LPCM 最低，FLAC 最高但抗 Wi-Fi 掉包；MoOde 用 WAV 可達 5 s | FLAC 16/24、WAV、RF64、LPCM | 可當「本機網路電台」`/stream/swyh.flac`；Sonos 用 WAV 可能卡頓、新韌體支援 FLAC；Chromecast 需經 BubbleUPnP Server 轉接。**與 AirSender 最接近的開源參考** |
| **AirMyPC** | Windows 7–11；US$27.95；v8.0 (2026-10-01) ([airmypc.com](https://www.airmypc.com/)) | AirPlay（含 HomePod、AirPlay 2 TV）、Google Cast、Roku、Sonos | 是（可只送音訊） | 未公開 | 未明示 | 商用閉源、持續更新 |
| **WinAirCast**（2025 新進） | Windows；US$2.99 終身，15 天試用 ([producthunt](https://www.producthunt.com/p/winaircast/winaircast)) | AirPlay 2（PTP 時鐘）＋ AirPlay 1 相容 | 是 | 宣稱「Real-time」約 45 ms（**廠商自稱，未驗證**） | 未明示 | HomePod 立體聲對；定位與 AirSender 幾乎相同，是最直接的競品 |
| **Shairport4w / ShairportQt** | Windows（Qt 版跨平台），開源 | AirPlay **接收端**（RAOP） | 否（方向相反：iPhone→PC） | — | — | Shairport4w 2022 停更，後繼 ShairportQt (2024-10) ([sourceforge](https://www.sourceforge.net/p/shairport4w/news))。可作 AirSender 的本機測試接收端 |
| **Windows 內建** | — | 無 AirPlay 送端。Win+K「投放」= **Miracast**，以螢幕鏡射為主，純音訊需搭配 Miracast 接收器 ([itechguides](https://www.itechguides.com/?p=349923))；Win11 另有 Bluetooth LE Audio「Shared Audio」但限 Copilot+ PC ([pureinfotech](https://pureinfotech.com/windows-11-shared-audio-feature/)) | — | — | — | iTunes for Windows 可對自家播放做 AirPlay；Apple Music for Windows 的 AirPlay→HomePod 有使用者回報不穩（未驗證） |
| **Snapcast** | server: Linux/macOS 為主；client 含 Windows (WASAPI)，GPL-3.0；v0.35.0 (2026-03-10) ([github](https://github.com/badaix/snapcast)) | 自有 TCP 協定；來源 pipe/tcp/airplay/librespot… | 是（需把 PC 音訊餵給 server） | server buffer 預設 1000 ms，可調 | PCM / FLAC / Opus | 多房間 <0.2 ms 同步；Windows client 無安裝程式，第三方 Snap.Net 提供 |
| **Roc Toolkit** | Linux / macOS / Android 官方支援，**無 Windows** ([docs](https://roc-streaming.org/toolkit/docs/portability/supported_platforms.html))；MPL-2.0；v0.4.0 (2024-06) | RTP + FEC (RS8M / LDPC) + RTCP | 是（PulseAudio/PipeWire 模組） | `--target-latency` 可到 50 ms 級 | PCM | Go/Java 綁定，無官方 Python |
| **Scream** | Windows 虛擬音效卡驅動，MS-PL ([github](https://github.com/duncanthrax/scream)) | UDP 多播 239.255.77.77:4010（可改單播），5-byte 標頭 + 1152 B raw PCM | 是 | 「核心層處理，延遲極低」（無數字） | raw PCM 任意取樣率/位元 | 接收端：Linux (ALSA/Pulse/JACK)、Windows ScreamReader、ESP32…；需另一端跑接收程式 |
| **VB-Audio VBAN** | Windows (Voicemeeter)、VBAN Receptor (iOS/Android)、Linux `vban` | UDP、原生 PCM，「發射器/接收器」模型 ([music-assistant](https://www.music-assistant.io/plugins/vban-receiver/)) | 是 | 數十 ms 級（接收端 quality 參數決定） | PCM | 需 Voicemeeter 或自寫接收端；協定規格公開 |
| **AudioRelay** | Windows/macOS/Linux → Android/iOS；Premium 約 US$14.99 終身（舊資料） ([audiorelay.net](https://audiorelay.net)) | 自有協定 (Wi-Fi/USB) | 是 | 宣稱低於 Bluetooth | 未明示 | 接收端是手機，不是喇叭 |
| **Sonos 桌面 App** | — | 僅控制器，**不轉送 PC 音訊** ([sonos community](https://en.community.sonos.com/questions/103666)) | 否 | Line-in（Five/Port/Amp）固定 ≥75 ms | — | 開源 RoomRelay 曾嘗試，延遲達數秒 |
| **Spotify Connect** | — | 閉源；只能播 Spotify 目錄內容，eSDK 僅授權硬體合作夥伴 ([spotify community](https://community.spotify.com/t5/Spotify-for-Developers/Spotify-API-Stream-Access-TOS/td-p/6273259)) | **不適用** | — | — | 無法送任意 PCM |

## B. 接收端協定總覽

| 協定 | 探索 | 傳送方式 | 格式 | 實際延遲 | 加密 / 認證 | Python 函式庫（授權・狀態） |
|---|---|---|---|---|---|---|
| **AirPlay 1 (RAOP)** | mDNS `_raop._tcp` | 送端 push RTP (UDP)，RTSP 控制 | 44.1k/16-bit ALAC 或 PCM | Realtime 模式典型約 2 s ([shairport-sync AIRPLAY2.md](https://git.sudo.is/mirrors/shairport-sync/src/branch/master/AIRPLAY2.md))；AirSender 自測 HomePod mini 約 520–550 ms | RSA/AES（舊式），Apple TV 4+ 需配對 | **pyatv** MIT，v0.18.0 (2026-06-19) ([releases](https://github.com/postlund/pyatv/releases))。文件：HomePod (mini)、第三方喇叭皆可；「HAP 加密尚未實作」([supported_features](https://pyatv.dev/documentation/supported_features/))——AirSender README 稱已走 HAP 通道，應是自行擴充 |
| **AirPlay 2** | mDNS `_airplay._tcp` (features bits) | PTP 時鐘、Buffered Audio (TCP) 或 Realtime | 同上；Buffered 模式典型 ≤0.5 s | HAP (SRP/Curve25519/ChaCha20)，HomePod 需 Home 設定「同網路所有人」 | pyatv（部分）；Music Assistant 內建 sender 同時支援 AP1/AP2 ([music-assistant](https://music-assistant.io/player-support/airplay/))；C 語言 libraop (philippe44) 有 VS2022 專案 |
| **Google Cast** | mDNS `_googlecast._tcp` | 接收端 **pull HTTP URL** (Default Media Receiver, `streamType=LIVE`, duration -1) ([cast live](https://developers.google.com/cast/docs/web_receiver/live)) | WAV (LPCM)、FLAC ≤96k/24、MP3、AAC、Opus ([supported media](https://developers.google.com/cast/docs/media)) | 接收端會大量預緩衝：mkchromecast 回報 2–4 s ([issue](https://github.com/muammar/mkchromecast/issues/61))；P-Cast HLS 約 3 s | 無（本地網路） | **pychromecast** MIT，v14.0.10 (2026-03-07)，需 Python ≥3.11 |
| **DLNA / UPnP AV (MediaRenderer)** | SSDP `urn:schemas-upnp-org:device:MediaRenderer:1` | 控制點呼叫 `SetAVTransportURI` + `Play`，接收端 pull HTTP；部分裝置要求 DIDL-Lite metadata ([sony](https://github.com/sonydevworld/audio_control_api_examples/blob/master/DLNA/AVTransport/play_file.adoc)) | WAV / LPCM / FLAC 常見，但各機韌體差異大；無限長串流需 chunked 或 RF64 | 1–5 s（看接收端緩衝；swyh-rs 經驗） | 無 | **async_upnp_client** Apache-2.0，v0.49.0 (2026-10-11)，HA 用於 DLNA DMR ([pypi](https://pypi.org/project/async-upnp-client/)) |
| **Sonos** | SSDP（UPnP 子集）＋ mDNS | 同 DLNA：`play_uri(url)` 讓喇叭 pull HTTP（WAV 可，新韌體 FLAC） | WAV/FLAC/MP3… | 數秒級；Line-in 固定 ≥75 ms ([sonos community](https://en.community.sonos.com/components-and-architectural-228996/amp-latency-question-6834036)) | 無（本地） | **SoCo** MIT，v0.32.0 (2026-10-15) ([releases](https://github.com/SoCo/SoCo/releases))；S1/S2 均可用 UPnP 控制。Sonos 多數現役機種（One、Beam、Arc、Era、Five、Play:5 g2、Port、Amp…）**本身即支援 AirPlay 2** ([sonos](https://support.sonos.com/article/stream-airplay-audio-to-sonos))，AirSender 現有路徑已可打 |
| **Snapcast** | 無（手動 host:1704） | PC 以 TCP 推 raw PCM 至 snapserver `tcp://` source；server 再對 client 時間同步分發 | PCM / FLAC / Opus | server buffer 預設 1000 ms，可降至數百 ms ([autogarsas](https://www.autogarsas.lt/en/blogs/namu-garso-sistemos-auto-audio-sprendimai/kas-yra-snapcast-ir-kaip-jis-veikia)) | 無 | 無需函式庫：純 socket；server GPL-3.0 但為獨立程序 |
| **Roc Toolkit** | 無（手動位址） | RTP + FEC + RTCP (UDP) | PCM | 數十 ms（`--target-latency 50ms`） | 無 | 無 Windows、無官方 Python；OpenFEC 之 LDPC 元件為 GPL 類授權 ([licensing](https://roc-streaming.org/toolkit/docs/about_project/licensing.html)) |
| **Bluetooth A2DP** | 非網路協定（點對點 2.4 GHz，無 IP） | — | SBC 有損；aptX LL 需雙邊支援 | SBC 約 200±50 ms；aptX LL 約 34–50 ms ([rtings](https://www.rtings.com/headphones/learn/latency)) | 配對 | Windows 由 OS 處理；非 AirSender 範疇 |
| **Spotify Connect** | mDNS `_spotify-connect._tcp` | 裝置自行向 Spotify 伺服器拉串流 | Ogg Vorbis | — | 需 Spotify 帳號；閉源 | librespot 僅能當**接收端**；無法送任意 PCM |
| **Miracast / Windows Cast** | Wi-Fi Direct / WFD | 螢幕 + 音訊 H.264 鏡射 | AAC/LPCM 隨影像 | 100–300 ms（鏡射） | WPS/WPA2 | 以影像為主；純音訊喇叭幾乎不支援；無 Python 路徑 |
| **Scream / VBAN** | 無（多播或固定 IP） | 送端 push UDP raw PCM | PCM | 數十 ms | 無 | 純 socket 即可送出；對面需裝接收程式 |
| **Amazon Echo** | — | 無公開 API；僅 Bluetooth 或 3.5 mm ([maketecheasier](https://maketecheasier.com/smart-home/use-amazon-echo-as-bluetooth-speaker)) | — | BT 150–200 ms | — | 不可行 |
| **Yamaha MusicCast / Denon HEOS / Bose** | — | 多數機種 2018–2019 起韌體支援 **AirPlay 2** ([9to5mac](https://9to5mac.com/2019/04/15/yamaha-airplay-2-updates/), [pooraudiophile](https://pooraudiophile.com/2018/09/denon-marantz-heos-airplay2-compatibility-list.html))；Yamaha、HEOS 亦為 DLNA DMR（swyh-rs 實測 Denon HEOS、Yamaha） | — | — | — | 已被 AirPlay 2 / DLNA 兩條路徑涵蓋 |

**Windows 擷取面注意**：WASAPI loopback 只能擷取 **shared mode** 混音；播放器用 **exclusive mode**（foobar2000 WASAPI exclusive、ASIO）或 DRM 受保護串流時擷取不到 ([Microsoft](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording))。替代：VB-Cable / Voicemeeter 之類虛擬裝置（使用者需手動改預設輸出），或 Windows 10 build 20348+ 的 **Process Loopback**（`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`，可只抓單一程式）([Microsoft](https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ne-audioclientactivationparams-process_loopback_mode))。另：loopback 在無人播放時不產生資料，需自行填靜音。

## C. 建議

### 優先順序（依「低延遲、無損、Windows、Python、系統匣」）

1. **一個 HTTP 即時 WAV/FLAC 伺服器 → 同時涵蓋 Google Cast、DLNA、Sonos(非 AirPlay 機種)、任何媒體播放器**
   - 實作：`aiohttp` 起 `/stream.wav`（chunked、無 Content-Length、44.1k/16-bit 標頭長度填 0xFFFFFFFF 或用 RF64）與 `/stream.flac`（可用 `pyflac` 或 `soundfile` 即時編碼；FLAC 抗 Wi-Fi 掉包、Sonos 新韌體偏好）。swyh-rs 已驗證此設計於 Windows 可行。
   - Cast：`pychromecast` → `mc.play_media(url, "audio/wav", stream_type="LIVE")` 後立即 `play()`；預期 2–4 s 延遲，無法做低延遲，應在 UI 標示「高延遲模式」。
   - DLNA：`async_upnp_client` SSDP 探索 + `DmrDevice.async_set_transport_uri(url, title, didl)` + `async_play()`；務必附 DIDL-Lite，並用 PC 在該子網的 IP。
   - Sonos：`SoCo.play_uri(url, title=...)`；S2 機種多半直接走 AirPlay 2 更低延遲，HTTP 路徑留給 Play:1/Play:3/S1。
   - 這條路徑**無損但高延遲**，和 AirPlay 低延遲路徑互補。

2. **Snapcast source（TCP push raw PCM）**
   - 最省工：對 `snapserver` 的 `tcp://0.0.0.0:4953?mode=server` 開 socket 寫入 48k 或 44.1k S16LE 即可；多房間同步由 Snapcast 處理，延遲由 server `buffer` 決定（可壓到 300–500 ms）。
   - 用途：Linux/樹莓派 DIY 多房間使用者。Server 不在 Windows 上跑，AirSender 只需實作 client-side push，不受 GPL 影響（獨立程序、socket 介面）。

3. **Scream / VBAN 送端**（可選、低成本）
   - 兩者皆是「UDP + 小標頭 + raw PCM」，各約 50 行 Python 即可送出；給已在用 ESP32 / Voicemeeter 接收端的進階用戶。延遲最低（數十 ms），但使用面窄。

4. **Roc Toolkit**：暫不建議——無 Windows 官方支援、無 Python 綁定、FEC 依賴有 GPL 類授權。

5. **不做**：Spotify Connect、Amazon Echo、Miracast、Bluetooth（OS 已處理）。

### 授權相容性（開源同時可能商售）

| 套件 | 授權（已查證） | 影響 |
|---|---|---|
| pyatv | MIT ([github](https://github.com/postlund/pyatv)) | 無限制 |
| pychromecast | MIT ([pypi](https://pypi.org/project/PyChromecast/)) | 無限制 |
| SoCo | MIT ([github](https://github.com/SoCo/SoCo)) | 無限制 |
| async_upnp_client | Apache-2.0 ([pypi](https://pypi.org/project/async-upnp-client/)) | 需保留 NOTICE；可商用 |
| PyAudioWPatch | MIT（承自 PyAudio） ([github](https://github.com/s0d3s/PyAudioWPatch)) | 無限制 |
| python-zeroconf | **LGPL-2.1-or-later**，v0.151.5 (2026-09-28) ([github](https://github.com/python-zeroconf/python-zeroconf)) | pyatv/pychromecast 皆依賴 |
| python-soxr（含 libsoxr） | **LGPL-2.1-or-later**，v1.1.0 (2026-05-03) ([pypi](https://pypi.org/project/soxr/)) | 以動態載入之 .pyd/.dll 形式隨 PyInstaller 打包 |
| pystray | **LGPL-3.0** ([github](https://github.com/moses-palmer/pystray))；GitHub 無正式 release 頁，PyPI 最後版 0.19.5（更新緩慢） | 同上 |
| Snapcast server | GPL-3.0 | 不隨 AirSender 散佈即無影響 |

LGPL 要點：商售閉源版本仍可使用，但須（a）保留 LGPL 授權聲明與原始碼取得方式，（b）讓使用者能替換該 LGPL 元件——PyInstaller 以獨立 .pyd/.dll 動態載入通常視為滿足，但**避免** `--onefile` 加密或靜態連結；（c）若修改了 libsoxr/zeroconf/pystray 本身，修改部分須以 LGPL 公開。保守做法：安裝程式附 `THIRD_PARTY_LICENSES.txt`，並保留 `_internal/` 目錄中的獨立 .pyd。若想徹底避開 LGPL：pystray 可換成 Windows 原生 Shell_NotifyIcon（`pywin32`，PSF/BSD 類）；soxr 可換 `scipy.signal.resample_poly`（BSD，但品質與可變速率功能較差）；zeroconf 無簡單替代。

### 未能驗證之項目
- WinAirCast 45 ms、TuneBlade 現況、AirParrot 現價、Apple Music for Windows 的 HomePod AirPlay 行為均為廠商或社群說法。
- pyatv 文件稱 HAP 加密未實作，與 AirSender README 所述「AirPlay 2 HAP 加密通道」不一致，請作者依自身實作確認。
- Google Cast 對無限長 chunked WAV 的實際相容性未找到官方文件，僅有社群成功案例（mkchromecast、swyh-rs 經 BubbleUPnP）。
