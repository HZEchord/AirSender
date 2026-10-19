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
from .config import Config, log_dir
from .sinks import HTTP_PULL_KINDS, KIND_LABELS
from .streamer import Device, State, Streamer
from .ui import ControlPanel, QuickPanel, UiThread
from .updater import Release, UpdateChecker, download, launch_installer

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
        self.selected_device: Optional[Device] = None
        self.capture_devices = []
        self._scanning = False
        self._last_notify = 0.0
        self.streamer = Streamer(self.config, self._on_state)
        self.ui = UiThread()
        self.panel = ControlPanel(self.ui, self)
        self.quick = QuickPanel(self.ui, self)
        self.updater = UpdateChecker(self.config, self._on_update_available)
        self._update_busy = ""
        self.icon = pystray.Icon(
            APP_NAME, make_icon_image(False), APP_NAME, menu=Menu(self._menu_items)
        )

    # ------------------------------------------------------------ 執行
    def run(self) -> None:
        self.streamer.start_loop()
        self.ui.start()
        dialogs.set_ui_thread(self.ui)
        try:
            self.icon.run(setup=self._setup)
        finally:
            self.streamer.shutdown()
            self.ui.stop()

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
        if self.config.update_check_days > 0:
            self.updater.start()

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
        self.panel.refresh()
        self.quick.refresh()

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

    def _status_lines(self) -> list[str]:
        """右鍵選單頂端的狀態（Win32 選單不支援換行，所以拆成多個項目）。"""
        st = self.streamer
        first = f"狀態：{st.state.value}"
        if st.device:
            first += f" → {st.device.name}（{KIND_LABELS.get(st.device.kind, st.device.kind)}）"
        lines = [first]
        if st.message and st.state != State.STREAMING:
            lines.append(st.message[:70])
        cap = st.capture_status
        if st.state == State.STREAMING and cap and cap.running:
            src = st.source
            if src is not None:
                lines.append(f"緩衝 {src.buffered_ms:.0f} ms　漂移 {src.drift_ppm:+.0f} ppm　{cap.source_rate} → {cap.target_rate} Hz")
            elif st.http.clients:
                lines.append("HTTP 接收端：" + "、".join(st.http.clients))
        return lines

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
                self.panel.refresh()
                self.quick.refresh()

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
            self._notify("找不到任何接收端，請先按「重新掃描」")
            return
        self._select_device(device)

    def _current_device(self) -> Optional[Device]:
        """目前選擇的接收端：串流中的 > 使用者在面板選的 > 設定檔記住的 > 清單第一台。"""
        if self.streamer.device:
            return self.streamer.device
        if self.selected_device is not None:
            match = next((d for d in self.devices if d.identifier == self.selected_device.identifier), None)
            if match is not None:
                return match
        for d in self.devices:
            if d.identifier == self.config.device_identifier:
                return d
        return self.devices[0] if self.devices else None

    def set_selected_device(self, device: Optional[Device]) -> None:
        self.selected_device = device

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
            self._notify("找不到任何接收端，請先按「重新掃描」")
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

    # ------------------------------------------------------------ 更新
    def _on_update_available(self, release: Release) -> None:
        self._notify(f"有新版本 v{release.version}，開啟快捷面板或控制面板按「更新」")
        self._update_menu()
        self.panel.refresh()
        self.quick.refresh()

    def _check_updates_now(self) -> None:
        self._update_busy = "檢查中…"
        self.panel.refresh()
        try:
            release = self.updater.check(force=True)
        finally:
            self._update_busy = ""
        if release is None:
            self._notify(self.updater.last_error or f"目前 v{__version__} 已是最新版本")
        self.panel.refresh()
        self.quick.refresh()

    def _update_now(self) -> None:
        release = self.updater.available
        if release is None or self._update_busy:
            return
        notes = release.notes.strip()
        if len(notes) > 600:
            notes = notes[:600] + "…"
        message = (
            f"更新到 v{release.version}？\n\n安裝程式約 {release.asset_size / 1048576:.0f} MB，"
            f"下載後會關閉 AirSender、靜默安裝並重新啟動。\n\n{notes}"
        )
        if not dialogs.ask_yes_no(message):
            return
        try:

            def progress(done: int, total: int) -> None:
                pct = f"{100 * done / total:.0f}%" if total else f"{done / 1048576:.0f} MB"
                self._update_busy = f"下載中 {pct}"
                self.icon.title = f"{APP_NAME}：下載更新 {pct}"

            self._update_busy = "下載中…"
            self.panel.refresh()
            path = download(release, progress)
            self._update_busy = "啟動安裝程式…"
            self.panel.refresh()
            if not launch_installer(path):
                raise RuntimeError("無法啟動安裝程式（可能已取消 UAC）")
            # 安裝程式會關閉本程式並在完成後重新啟動；主動結束比較乾淨
            self._quit()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("更新失敗：%s", exc)
            self._update_busy = ""
            dialogs.show_info(f"更新失敗：{exc}")
            self.panel.refresh()

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
                self.updater.stop()
                self.streamer.shutdown()
            finally:
                self.ui.stop()
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


    def _menu_items(self):
        """右鍵選單只留最常用的；左鍵（預設項目）開快捷面板，其餘設定在控制面板。"""
        for line in self._status_lines():
            yield MenuItem(line, None, enabled=False)
        yield Menu.SEPARATOR
        yield MenuItem(
            lambda item: f"更新到 v{self.updater.available.version}…" if self.updater.available else "更新",
            _call(lambda: threading.Thread(target=self._update_now, daemon=True).start()),
            visible=lambda item: self.updater.available is not None,
        )
        yield MenuItem("快捷面板", _call(self.quick.toggle), default=True)
        yield MenuItem("控制面板…", _call(self.panel.show))
        yield MenuItem(
            lambda item: "停止串流" if self.streamer.is_active else "開始串流",
            lambda icon, item: self._toggle_stream(),
        )
        yield MenuItem("接收端", Menu(self._device_items))
        yield Menu.SEPARATOR
        yield MenuItem("結束", lambda icon, item: self._quit())


def run_tray() -> None:
    TrayApp().run()
