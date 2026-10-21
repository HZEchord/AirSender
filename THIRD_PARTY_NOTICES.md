# 第三方元件授權聲明

AirSender 本身以 MIT 授權釋出（見 LICENSE）。打包版（`dist\AirSender\`、安裝程式）內含下列第三方元件，
各依其原始授權散佈；完整授權全文隨各套件安裝於 `_internal\` 目錄或可由下列連結取得。

| 元件 | 用途 | 授權 | 來源 |
| --- | --- | --- | --- |
| Python 3.13 | 執行環境 | PSF License | https://www.python.org/ |
| pyatv | AirPlay / RAOP / HAP 協定 | MIT | https://github.com/postlund/pyatv |
| PyAudioWPatch（含 PortAudio） | WASAPI loopback 擷取 | MIT | https://github.com/s0d3s/PyAudioWPatch |
| python-soxr（含 libsoxr） | 重取樣 | **LGPL-2.1-or-later** | https://github.com/dofuuz/python-soxr |
| NumPy | 數值運算 | BSD-3-Clause | https://numpy.org/ |
| pystray | 系統匣 | **LGPL-3.0** | https://github.com/moses-palmer/pystray |
| Pillow | 圖示繪製 | MIT-CMU (HPND) | https://python-pillow.org/ |
| python-zeroconf | mDNS 探索（pyatv / pychromecast 依賴） | **LGPL-2.1-or-later** | https://github.com/python-zeroconf/python-zeroconf |
| aiohttp | HTTP 串流伺服器 | Apache-2.0 | https://github.com/aio-libs/aiohttp |
| PyChromecast | Google Cast 控制 | MIT | https://github.com/home-assistant-libs/pychromecast |
| async-upnp-client | DLNA / UPnP 控制 | Apache-2.0 | https://github.com/StevenLooman/async_upnp_client |
| pycaw + comtypes | 串流時靜音電腦輸出端點（Windows Core Audio） | MIT | https://github.com/AndreMiras/pycaw 、 https://github.com/enthought/comtypes |
| cryptography | HAP 加密（pyatv 依賴） | Apache-2.0 / BSD-3-Clause | https://github.com/pyca/cryptography |
| miniaudio（pyatv 依賴） | 音訊解碼 | MIT | https://github.com/irmen/pyminiaudio |
| pydantic、protobuf、requests、srptools、chacha20poly1305-reuseable、tabulate、ifaddr 等 | pyatv / pychromecast 依賴 | MIT / BSD / Apache-2.0 | 見各套件 |
| PyInstaller（建置工具，執行階段只含 bootloader） | 打包 | GPL-2.0 with Bootloader Exception | https://pyinstaller.org/ |
| Inno Setup（建置工具） | 安裝程式 | Inno Setup License | https://jrsoftware.org/ |

## LGPL 元件的注意事項

AirSender 本身免費公開、不販售，以下說明供任何再散佈者參考。

python-soxr（libsoxr）、pystray、python-zeroconf 為 LGPL。LGPL 允許在閉源 / 商業軟體中使用，但散佈時須：

1. 保留這些元件的授權聲明與取得原始碼的方式（本檔案與 `_internal\` 內的 LICENSE 檔即為此用）。
2. 讓使用者能替換該元件：AirSender 以 PyInstaller **onedir** 模式打包，這些元件以獨立的 `.pyd` / `.dll` /
   `.py` 檔存在於 `_internal\`，可直接替換。**請勿**改用 onefile 加密打包或靜態連結。
3. 若修改了這些元件本身，修改部分須以 LGPL 公開。AirSender 未修改它們。

PyInstaller 的 bootloader 例外條款允許打包後的程式以任何授權散佈。
