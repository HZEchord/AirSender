"""系統匣介面（pystray）。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Optional

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from . import APP_NAME, __version__, autostart, dialogs, firewall
from .capture import list_loopback_devices
from .config import DRIFT_MODES, LATENCY_PRESETS, RESAMPLE_QUALITIES, Config, log_dir
from .sinks import HTTP_PULL_KINDS, KIND_LABELS
from .streamer import Device, State, Streamer

_LOGGER = logging.getLogger(__name__)

JITTER_PRESETS = {"20 ms": 20, "40 ms": 40, "80 ms": 80, "150 ms": 150}
VOLUME_STEPS = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]


def _call(fn, *args):
    """pystray 只接受 0/1/2 個參數的回呼，用閉包綁定額外參數。"""
    return lambda icon, item: fn(*args)


def _check(fn):
    """包裝 checked 回呼為單一參數 (item) 的函式。"""
    return lambda item: fn()


def make_icon_image(active: bool, size: int = 64) -> Image.Image:
    """畫一個喇叭 + 三道聲波的圖示；串流中為藍色，閒置為灰色。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (0, 122, 255, 255) if active else (150, 150, 150, 255)
    s = size / 64.0
    # 喇叭本體
    draw.rectangle([10 * s, 24 * s, 20 * s, 40 * s], fill=color)
    draw.polygon(
        [(20 * s, 24 * s), (32 * s, 14 * s), (32 * s, 50 * s), (20 * s, 40 * s)],
        fill=color,
    )
    # 聲波
    for r in (10, 17, 24):
        box = [32 * s - r * s, 32 * s - r * s, 32 * s + r * s, 32 * s + r * s]
        draw.arc(box, start=-40, end=40, fill=color, width=max(2, int(4 * s)))
    return img


class TrayApp:
    def __init__(self) -> None:
        self.config = Config.load()
        self.devices: list[Device] = []
        self.capture_devices = []
        self._scanning = False
        self._last_notify = 0.0
        self.streamer = Streamer(self.config, self._on_state)
        self.icon = pystray.Icon(
            APP_NAME, make_icon_image(False), APP_NAME, menu=Menu(self._menu_items)
        )

    # ------------------------------------------------------------ 執行
    def run(self) -> None:
        self.streamer.start_loop()
        try:
            self.icon.run(setup=self._setup)
        finally:
            self.streamer.shutdown()

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        self._refresh_capture_devices()
        if getattr(sys, "frozen", False) and not firewall.rules_present():
            self._notify(
                "尚未允許 AirSender 通過 Windows 防火牆，HomePod 可能連不上。"
                "請在選單選「允許通過 Windows 防火牆」。"
            )
        self._rescan_async(
            then_autostart=self.config.autostart_stream
            and bool(self.config.device_identifier)
        )

    # ------------------------------------------------------------ 狀態
    def _on_state(self, state: State, message: str) -> None:
        active = state == State.STREAMING
        self.icon.icon = make_icon_image(active)
        tip = f"{APP_NAME}：{state.value}"
        if message:
            tip += f" - {message}"
        self.icon.title = tip[:127]
        if state == State.RECONNECTING and time.monotonic() - self._last_notify > 30:
            self._last_notify = time.monotonic()
            self._notify(message or "連線中斷，重新連線中")
        self._update_menu()

    def _notify(self, message: str, title: str = APP_NAME) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:  # pylint: disable=broad-except
            pass

    def _update_menu(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:  # pylint: disable=broad-except
            pass

    def _status_text(self) -> str:
        text = f"狀態：{self.streamer.state.value}"
        if self.streamer.message:
            text += f" - {self.streamer.message}"
        if self.streamer.device:
            text += f"\n接收端：{KIND_LABELS.get(self.streamer.device.kind, self.streamer.device.kind)}"
        cap = self.streamer.capture_status
        if self.streamer.state == State.STREAMING and cap and cap.running:
            text += f"\n來源：{cap.device_name} {cap.source_rate} Hz -> {cap.target_rate} Hz"
            src = self.streamer.source
            if src is not None:
                text += f"\n本機緩衝 {src.buffered_ms:.0f} ms，漂移補償 {src.drift_ppm:+.0f} ppm"
            elif self.streamer.http.clients:
                text += f"\nHTTP 接收端：{', '.join(self.streamer.http.clients)}"
        return text

    # ------------------------------------------------------------ 掃描
    def _rescan_async(self, then_autostart: bool = False) -> None:
        if self._scanning:
            return
        self._scanning = True
        self._update_menu()

        def worker() -> None:
            try:
                self.devices = self.streamer.scan(timeout=5.0)
                _LOGGER.info("找到 %d 個接收端", len(self.devices))
                if then_autostart:
                    saved = next(
                        (
                            d
                            for d in self.devices
                            if d.identifier == self.config.device_identifier
                        ),
                        None,
                    )
                    if saved is None and self.config.device_kind == "snapcast":
                        saved = self.streamer.saved_device()
                    if saved:
                        self.streamer.start(saved)
                    elif self.config.device_name:
                        self._notify(f"找不到上次的裝置 {self.config.device_name}")
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning("掃描失敗：%s", exc)
                self._notify(f"掃描失敗：{exc}")
            finally:
                self._scanning = False
                self._update_menu()

        threading.Thread(target=worker, name="scan", daemon=True).start()

    def _refresh_capture_devices(self) -> None:
        try:
            self.capture_devices = list_loopback_devices()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("列出輸出裝置失敗：%s", exc)
            self.capture_devices = []

    # ------------------------------------------------------------ 動作
    def _select_device(self, device: Device) -> None:
        def worker() -> None:
            try:
                self.streamer.start(device)
            except Exception as exc:  # pylint: disable=broad-except
                self._notify(f"無法開始串流：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_stream(self) -> None:
        if self.streamer.is_active:
            threading.Thread(target=self.streamer.stop, daemon=True).start()
            return
        device = self._current_device()
        if device is None:
            self._notify("請先在「接收端」選擇裝置")
            return
        self._select_device(device)

    def _current_device(self) -> Optional[Device]:
        if self.streamer.device:
            return self.streamer.device
        for d in self.devices:
            if d.identifier == self.config.device_identifier:
                return d
        return None

    def _apply_and_restart(self) -> None:
        self.config.save()
        threading.Thread(target=self.streamer.restart, daemon=True).start()

    def _set_latency(self, ms: int) -> None:
        self.config.receiver_latency_ms = ms
        self._apply_and_restart()

    def _set_jitter(self, ms: int) -> None:
        self.config.jitter_buffer_ms = ms
        self._apply_and_restart()

    def _set_quality(self, quality: str) -> None:
        self.config.resample_quality = quality
        self._apply_and_restart()

    def _toggle_dither(self) -> None:
        self.config.dither = not self.config.dither
        self._apply_and_restart()

    def _set_drift_mode(self, mode: str) -> None:
        self.config.drift_compensation = mode
        self._apply_and_restart()

    def _set_capture_device(self, index: Optional[int]) -> None:
        self.config.capture_device_index = index
        self._apply_and_restart()

    def _set_volume(self, percent: Optional[int]) -> None:
        if percent is None:
            self.config.volume = None
            self.config.save()
        else:
            self.streamer.set_volume(percent)

    def _reconnect(self) -> None:
        device = self._current_device()
        if device is None:
            self._notify("請先在「接收端」選擇裝置")
            return
        self._select_device(device)

    def _volume_step(self, delta: int) -> None:
        def worker() -> None:
            new = self.streamer.volume_step(delta)
            if new is not None:
                self.icon.title = f"{APP_NAME}：音量 {new}%"
                self._update_menu()

        threading.Thread(target=worker, daemon=True).start()

    def _pair_current(self) -> None:
        device = self._current_device()
        if device is None:
            self._notify("請先在「接收端」選擇裝置")
            return

        def worker() -> None:
            try:
                self._notify(f"開始與 {device.name} 配對…")
                results = self.streamer.pair(device, dialogs.ask_string, self._notify)
                ok = [k.upper() for k, v in results.items() if v]
                failed = [k.upper() for k, v in results.items() if not v]
                summary = []
                if ok:
                    summary.append(f"成功：{', '.join(ok)}")
                if failed:
                    summary.append(f"失敗：{', '.join(failed)}")
                dialogs.show_info(
                    f"{device.name} 配對結果\n" + ("\n".join(summary) or "沒有可配對的協定")
                )
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning("配對失敗：%s", exc)
                dialogs.show_info(f"配對失敗：{exc}")
            finally:
                self._update_menu()

        threading.Thread(target=worker, name="pair", daemon=True).start()

    def _set_password_current(self) -> None:
        device = self._current_device()
        if device is None:
            self._notify("請先在「接收端」選擇裝置")
            return

        def worker() -> None:
            password = dialogs.ask_string(
                f"請輸入 {device.name} 的 AirPlay 密碼（留空以清除）", secret=True
            )
            if password is None:
                return
            self.streamer.set_password(device, password or None)
            self._notify("已更新密碼" if password else "已清除密碼")
            self._update_menu()

        threading.Thread(target=worker, daemon=True).start()

    def _forget_current(self) -> None:
        device = self._current_device()
        if device is None:
            return
        self.streamer.forget_credentials(device)
        self._notify(f"已清除 {device.name} 的配對與密碼")
        self._update_menu()

    def _toggle_autostart_stream(self) -> None:
        self.config.autostart_stream = not self.config.autostart_stream
        self.config.save()

    def _toggle_autostart_windows(self) -> None:
        autostart.set_enabled(not autostart.is_enabled())

    def _add_firewall_rules(self) -> None:
        if firewall.add_rules():
            self._notify("已送出防火牆規則設定（需在 UAC 視窗按「是」）")
        else:
            self._notify("未能加入防火牆規則（可能已取消 UAC）")

    def _open_logs(self) -> None:
        try:
            os.startfile(str(log_dir()))  # type: ignore[attr-defined]
        except Exception:  # pylint: disable=broad-except
            subprocess.Popen(["explorer", str(log_dir())])

    def _add_snapcast(self) -> None:
        def worker() -> None:
            spec = dialogs.ask_string(
                "請輸入 snapserver 位址（host 或 host:port，預設 port 4953）。\n"
                "snapserver.conf 需設定：\n"
                "source = tcp://0.0.0.0:4953?name=AirSender&mode=server&sampleformat=44100:16:2"
            )
            if not spec:
                return
            try:
                device = self.streamer.add_snapcast_server(spec.strip())
            except ValueError:
                self._notify("位址格式錯誤")
                return
            self.devices = [d for d in self.devices if d.identifier != device.identifier] + [device]
            self._update_menu()

        threading.Thread(target=worker, daemon=True).start()

    def _remove_snapcast(self, spec: str) -> None:
        self.streamer.remove_snapcast_server(spec)
        self.devices = [d for d in self.devices if not (d.kind == "snapcast" and d.identifier.endswith(spec if ":" in spec else f"{spec}:4953"))]
        self._update_menu()

    def _quit(self) -> None:
        def worker() -> None:
            try:
                self.streamer.shutdown()
            finally:
                self.icon.stop()

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ 選單
    def _device_items(self):
        if self._scanning:
            yield MenuItem("掃描中…", None, enabled=False)
        elif not self.devices:
            yield MenuItem("找不到任何接收端", None, enabled=False)
        for kind, label in KIND_LABELS.items():
            group = [d for d in self.devices if d.kind == kind]
            if not group:
                continue
            note = "（HTTP 拉流，延遲 2–5 秒）" if kind in HTTP_PULL_KINDS else ""
            yield MenuItem(f"{label}{note}", None, enabled=False)
            for d in group:
                yield MenuItem(
                    "    " + d.label,
                    _call(self._select_device, d),
                    checked=_check(lambda dev=d: (
                        self.streamer.device is not None
                        and self.streamer.device.identifier == dev.identifier
                    )),
                    radio=True,
                )
        yield Menu.SEPARATOR
        yield MenuItem("重新掃描", lambda icon, item: self._rescan_async())
        yield MenuItem("新增 Snapcast 伺服器…", _call(self._add_snapcast))
        for spec in list(self.config.snapcast_servers):
            yield MenuItem(f"移除 Snapcast {spec}", _call(self._remove_snapcast, spec))

    def _latency_items(self):
        for label, ms in LATENCY_PRESETS.items():
            yield MenuItem(
                label,
                _call(self._set_latency, ms),
                checked=_check(lambda v=ms: self.config.receiver_latency_ms == v),
                radio=True,
            )
        yield Menu.SEPARATOR
        yield MenuItem("本機抖動緩衝", Menu(self._jitter_items))

    def _jitter_items(self):
        for label, ms in JITTER_PRESETS.items():
            yield MenuItem(
                label,
                _call(self._set_jitter, ms),
                checked=_check(lambda v=ms: self.config.jitter_buffer_ms == v),
                radio=True,
            )

    def _quality_items(self):
        yield MenuItem("時鐘漂移補償", None, enabled=False)
        for label, mode in DRIFT_MODES.items():
            yield MenuItem(
                label,
                _call(self._set_drift_mode, mode),
                checked=_check(lambda v=mode: self.config.drift_compensation == v),
                radio=True,
            )
        yield Menu.SEPARATOR
        yield MenuItem("固定比率重取樣品質", None, enabled=False)
        for label, q in RESAMPLE_QUALITIES.items():
            yield MenuItem(
                label,
                _call(self._set_quality, q),
                checked=_check(lambda v=q: self.config.resample_quality == v),
                radio=True,
            )
        yield Menu.SEPARATOR
        yield MenuItem(
            "TPDF dither（轉 16-bit 時）",
            lambda icon, item: self._toggle_dither(),
            checked=lambda item: self.config.dither,
        )

    def _capture_items(self):
        yield MenuItem(
            "跟隨 Windows 預設輸出",
            lambda icon, item: self._set_capture_device(None),
            checked=lambda item: self.config.capture_device_index is None,
            radio=True,
        )
        for index, name, rate in self.capture_devices:
            yield MenuItem(
                f"{name}（{rate} Hz）",
                _call(self._set_capture_device, index),
                checked=_check(lambda i=index: self.config.capture_device_index == i),
                radio=True,
            )
        yield Menu.SEPARATOR
        yield MenuItem(
            "重新整理",
            _call(lambda: (self._refresh_capture_devices(), self._update_menu())),
        )

    def _volume_items(self):
        yield MenuItem(
            "不控制接收端音量",
            lambda icon, item: self._set_volume(None),
            checked=lambda item: self.config.volume is None,
            radio=True,
        )
        yield Menu.SEPARATOR
        for v in VOLUME_STEPS:
            yield MenuItem(
                f"{v}%",
                _call(self._set_volume, v),
                checked=_check(lambda p=v: self.config.volume == p),
                radio=True,
            )

    def _pairing_items(self):
        device = self._current_device()
        name = device.name if device else "（尚未選擇接收端）"
        is_airplay = device is not None and device.kind == "airplay"
        has = bool(device and self.streamer.has_credentials(device))
        yield MenuItem(f"目前接收端：{name}", None, enabled=False)
        yield MenuItem(
            f"配對…{'（已配對）' if has else ''}",
            _call(self._pair_current),
            enabled=is_airplay,
        )
        yield MenuItem("設定接收端密碼…", _call(self._set_password_current), enabled=is_airplay)
        yield MenuItem("忘記配對與密碼", _call(self._forget_current), enabled=has)
        yield Menu.SEPARATOR
        yield MenuItem("HomePod 不需要配對；Apple TV 開啟「需要裝置驗證」或第三方接收端設了密碼時才需要", None, enabled=False)

    def _menu_items(self):
        yield MenuItem(lambda item: self._status_text(), None, enabled=False)
        yield Menu.SEPARATOR
        yield MenuItem(
            lambda item: "停止串流" if self.streamer.is_active else "開始串流",
            lambda icon, item: self._toggle_stream(),
            default=True,
        )
        yield MenuItem(
            "重新連線",
            _call(self._reconnect),
            enabled=lambda item: self._current_device() is not None,
        )
        yield MenuItem(
            "音量 +5",
            _call(self._volume_step, 5),
            enabled=lambda item: self.streamer.is_active,
        )
        yield MenuItem(
            "音量 -5",
            _call(self._volume_step, -5),
            enabled=lambda item: self.streamer.is_active,
        )
        yield Menu.SEPARATOR
        yield MenuItem("接收端", Menu(self._device_items))
        yield MenuItem("接收端音量", Menu(self._volume_items))
        yield MenuItem("配對與密碼", Menu(self._pairing_items))
        yield Menu.SEPARATOR
        yield MenuItem("延遲", Menu(self._latency_items))
        yield MenuItem("音質", Menu(self._quality_items))
        yield MenuItem("擷取來源", Menu(self._capture_items))
        yield Menu.SEPARATOR
        yield MenuItem(
            "啟動後自動連線到上次裝置",
            lambda icon, item: self._toggle_autostart_stream(),
            checked=lambda item: self.config.autostart_stream,
        )
        yield MenuItem(
            "登入 Windows 時自動啟動",
            lambda icon, item: self._toggle_autostart_windows(),
            checked=lambda item: autostart.is_enabled(),
        )
        yield MenuItem(
            "允許通過 Windows 防火牆（需系統管理員）", _call(self._add_firewall_rules)
        )
        yield MenuItem("開啟記錄資料夾", lambda icon, item: self._open_logs())
        yield Menu.SEPARATOR
        yield MenuItem(f"{APP_NAME} v{__version__}", None, enabled=False)
        yield MenuItem("結束", lambda icon, item: self._quit())


def run_tray() -> None:
    TrayApp().run()
