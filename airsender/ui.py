"""tkinter 介面：UI 執行緒、快捷面板（左鍵）與控制面板。

pystray 佔用主執行緒跑 Win32 訊息迴圈，tkinter 則在自己的執行緒擁有一個隱藏的根視窗；
所有 Tk 操作都經由 UiThread.call() 送到該執行緒執行（Tk 不是執行緒安全的）。

- QuickPanel：左鍵點系統匣圖示彈出的小視窗，停在右下角，點了不會關；
  點別處或按 Esc 才收起。放常用操作：開始/停止、接收端、音量、延遲。
- ControlPanel：完整設定視窗（右鍵選單「開啟控制面板…」或快捷面板的按鈕開啟）。

兩個視窗與右鍵選單共用同一份 Config；視窗每秒從 Config 同步（拖曳滑桿時除外），
所以不論在哪裡改設定，三處顯示永遠一致。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Optional

from . import APP_NAME, __version__, autostart
from .config import DRIFT_MODES, LATENCY_PRESETS, RESAMPLE_QUALITIES, latency_preset_name
from .sinks import HTTP_PULL_KINDS, KIND_LABELS
from .streamer_types import State

if TYPE_CHECKING:  # pragma: no cover
    from .tray import TrayApp

_LOGGER = logging.getLogger(__name__)

UI_FONT = "Microsoft JhengHei UI"


class UiThread:
    """擁有 Tk 根視窗的執行緒；其他執行緒以 call() 排程工作。"""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="ui", daemon=True)
        self._ready = threading.Event()
        self.root: Any = None
        self.scale = 1.0  # 相對於 96 DPI 的縮放（wraplength 等像素值用）

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(10)

    def stop(self) -> None:
        if self.root is not None:
            self.call(self.root.quit)

    def _run(self) -> None:
        import tkinter as tk  # pylint: disable=import-outside-toplevel
        from tkinter import ttk  # pylint: disable=import-outside-toplevel

        root = tk.Tk()
        root.withdraw()
        root.title(APP_NAME)
        root.option_add("*Font", f"{{{UI_FONT}}} 10")
        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=(UI_FONT, 10))
        style.configure("Title.TLabel", font=(UI_FONT, 13, "bold"))
        style.configure("Quick.TLabel", font=(UI_FONT, 11, "bold"))
        style.configure("Muted.TLabel", foreground="#666666")
        style.configure("Big.TButton", font=(UI_FONT, 11, "bold"), padding=(16, 6))
        style.configure("Seg.Toolbutton", padding=(10, 3))
        self.scale = float(root.tk.call("tk", "scaling")) / (96 / 72)
        self.root = root
        self._ready.set()
        root.after(30, self._poll)
        root.mainloop()

    def _poll(self) -> None:
        while True:
            try:
                fn, args, future = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                result = fn(*args)
                if future is not None:
                    future.set_result(result)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.exception("UI 工作失敗")
                if future is not None:
                    future.set_exception(exc)
        self.root.after(30, self._poll)

    def call(self, fn: Callable, *args) -> Future:
        future: Future = Future()
        if threading.current_thread() is self._thread:
            try:
                future.set_result(fn(*args))
            except Exception as exc:  # pylint: disable=broad-except
                future.set_exception(exc)
            return future
        self._queue.put((fn, args, future))
        return future

    def call_sync(self, fn: Callable, *args, timeout: Optional[float] = None):
        return self.call(fn, *args).result(timeout)

    def px(self, logical: int) -> int:
        return int(logical * self.scale)

    # ------------------------------------------------------------ 對話框
    def ask_string(self, prompt: str, title: str = APP_NAME, *, secret: bool = False) -> Optional[str]:
        from tkinter import simpledialog  # pylint: disable=import-outside-toplevel

        def _ask():
            self.root.attributes("-topmost", True)
            try:
                return simpledialog.askstring(title, prompt, parent=self.root, show="*" if secret else None)
            finally:
                self.root.attributes("-topmost", False)

        return self.call_sync(_ask)

    def show_info(self, message: str, title: str = APP_NAME) -> None:
        from tkinter import messagebox  # pylint: disable=import-outside-toplevel

        self.call_sync(lambda: messagebox.showinfo(title, message, parent=self.root))

    def ask_yes_no(self, message: str, title: str = APP_NAME) -> bool:
        from tkinter import messagebox  # pylint: disable=import-outside-toplevel

        def _ask():
            self.root.attributes("-topmost", True)
            try:
                return bool(messagebox.askyesno(title, message, parent=self.root))
            finally:
                self.root.attributes("-topmost", False)

        return bool(self.call_sync(_ask))


def _run_bg(fn: Callable, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


class _PanelBase:
    """兩個面板共用：接收端清單、狀態文字、與 Config 同步、音量 / 延遲套用。"""

    LATENCY_MIN, LATENCY_MAX, LATENCY_STEP = 100, 2000, 50
    JITTER_MIN, JITTER_MAX, JITTER_STEP = 10, 150, 5
    LMIN_MIN, LMIN_MAX, LMIN_STEP = 10, 250, 5

    def __init__(self, ui: UiThread, app: TrayApp) -> None:
        self.ui = ui
        self.app = app
        self.win: Any = None
        self._vars: dict[str, Any] = {}
        self._device_keys: list[str] = []
        self._dragging = False
        self._volume_job: Any = None
        self._tick_job: Any = None

    # ------------------------------------------------------------ 執行緒安全入口
    def show(self) -> None:
        self.ui.call(self._show)

    def refresh(self) -> None:
        if self.win is not None:
            self.ui.call(self._refresh_all)

    def _show(self) -> None:  # pragma: no cover - 由子類別實作
        raise NotImplementedError

    def _visible(self) -> bool:
        return self.win is not None and self.win.winfo_exists() and self.win.state() != "withdrawn"

    # ------------------------------------------------------------ 共用資料
    def _device_names(self) -> tuple[list[str], list[str]]:
        names: list[str] = []
        keys: list[str] = []
        for kind, label in KIND_LABELS.items():
            for d in self.app.devices:
                if d.kind != kind:
                    continue
                note = "，延遲 2–5 秒" if kind in HTTP_PULL_KINDS else ""
                names.append(f"[{label}{note}] {d.name}（{d.model}，{d.address}）")
                keys.append(d.identifier)
        if self.app._scanning:  # pylint: disable=protected-access
            names.insert(0, "掃描中…")
            keys.insert(0, "")
        elif not names:
            names.append("找不到接收端，請按「重新掃描」")
            keys.append("")
        return names, keys

    def _fill_device_combo(self, combo) -> None:
        names, keys = self._device_names()
        self._device_keys = keys
        combo["values"] = names
        current = self.app.streamer.device or self.app._current_device()  # pylint: disable=protected-access
        if current and current.identifier in keys:
            combo.current(keys.index(current.identifier))
        elif names:
            combo.current(0)

    def _on_device_selected(self, combo) -> None:
        idx = combo.current()
        if idx < 0 or idx >= len(self._device_keys) or not self._device_keys[idx]:
            return
        key = self._device_keys[idx]
        device = next((d for d in self.app.devices if d.identifier == key), None)
        if device is not None:
            self.app._select_device(device)  # pylint: disable=protected-access

    def _status_lines(self) -> tuple[str, str]:
        st = self.app.streamer
        device = st.device or self.app._current_device()  # pylint: disable=protected-access
        line = st.state.value
        if device:
            line += f" → {device.name}（{KIND_LABELS.get(device.kind, device.kind)}）"
        parts = []
        if st.message and st.state != State.STREAMING:
            parts.append(st.message)
        cap = st.capture_status
        if st.state == State.STREAMING and cap and cap.running:
            parts.append(f"來源 {cap.device_name} {cap.source_rate} Hz → {cap.target_rate} Hz / 16-bit")
            src = st.source
            if src is not None:
                parts.append(
                    f"本機緩衝 {src.buffered_ms:.0f} ms　漂移補償 {src.drift_ppm:+.0f} ppm　"
                    f"underrun {src.underruns}　輸入溢位 {cap.overflows}"
                )
            elif st.http.clients:
                parts.append("HTTP 接收端：" + "、".join(st.http.clients))
        return line, "\n".join(parts)

    # ------------------------------------------------------------ 與 Config 同步
    def _sync_from_config(self) -> None:
        """把 Config 的值寫進本視窗的變數（右鍵選單或另一個視窗改了設定時保持一致）。"""
        if self._dragging:
            return
        cfg = self.app.config
        v = self._vars
        if "volume" in v:
            v["volume_off"].set(cfg.volume is None)
            if cfg.volume is not None:
                v["volume"].set(float(cfg.volume))
        if "latency" in v:
            v["latency"].set(float(cfg.receiver_latency_ms))
        if "jitter" in v:
            v["jitter"].set(float(cfg.jitter_buffer_ms))
        if "lmin" in v:
            v["lmin"].set(float(cfg.airplay_latency_min_ms))
        if "preset" in v:
            v["preset"].set(latency_preset_name(cfg))
        if "quick_latency" in v:
            v["quick_latency"].set(latency_preset_name(cfg))
        if "drift" in v:
            v["drift"].set(cfg.drift_compensation)
        if "quality" in v:
            v["quality"].set(next((k for k, q in RESAMPLE_QUALITIES.items() if q == cfg.resample_quality), "最高 (VHQ)"))
        if "dither" in v:
            v["dither"].set(cfg.dither)
        if "auto_stream" in v:
            v["auto_stream"].set(cfg.autostart_stream)
        if "auto_win" in v:
            v["auto_win"].set(autostart.is_enabled())
        if "toggle_text" in v:
            v["toggle_text"].set("停止串流" if self.app.streamer.is_active else "開始串流")
        self._update_labels()

    def _update_labels(self) -> None:
        v = self._vars
        if "volume_label" in v:
            v["volume_label"].set("—" if v["volume_off"].get() else f"{int(round(v['volume'].get()))}%")
        if "latency_label" in v:
            v["latency_label"].set(f"{self._snap(v['latency'].get(), self.LATENCY_STEP)} ms")
        if "jitter_label" in v:
            v["jitter_label"].set(f"{self._snap(v['jitter'].get(), self.JITTER_STEP)} ms")
        if "lmin_label" in v:
            v["lmin_label"].set(f"{self._snap(v['lmin'].get(), self.LMIN_STEP)} ms")

    def _update_text(self) -> str:
        rel = self.app.updater.available
        if rel is not None:
            return f"有新版本 v{rel.version}，點「更新」下載安裝"
        return ""

    @staticmethod
    def _snap(value: float, step: int) -> int:
        return int(round(value / step) * step)

    def _bind_drag(self, scale) -> None:
        scale.bind("<ButtonPress-1>", lambda _e: setattr(self, "_dragging", True), add="+")

    # ------------------------------------------------------------ 共用動作
    def _toggle(self) -> None:
        _run_bg(self.app._toggle_stream)  # pylint: disable=protected-access

    def _on_volume_move(self, _value=None) -> None:
        if self._vars["volume_off"].get():
            self._vars["volume_off"].set(False)
        self._update_labels()
        if self._volume_job is None:
            self._volume_job = self.win.after(150, self._apply_volume)

    def _apply_volume(self) -> None:
        self._volume_job = None
        if self._vars["volume_off"].get():
            return
        pct = int(round(self._vars["volume"].get()))
        _run_bg(self.app.streamer.set_volume, pct)

    def _on_volume_release(self) -> None:
        self._dragging = False
        self._apply_volume()

    def _on_volume_off(self) -> None:
        if self._vars["volume_off"].get():
            self.app._set_volume(None)  # pylint: disable=protected-access
            self._update_labels()
        else:
            self._apply_volume()

    def _apply_preset(self, name: str) -> None:
        lmin, lat, jit = LATENCY_PRESETS[name]
        cfg = self.app.config
        if (lmin, lat, jit) != (cfg.airplay_latency_min_ms, cfg.receiver_latency_ms, cfg.jitter_buffer_ms):
            cfg.airplay_latency_min_ms, cfg.receiver_latency_ms, cfg.jitter_buffer_ms = lmin, lat, jit
            self.app._apply_and_restart()  # pylint: disable=protected-access
        self._sync_from_config()

    def _apply_latency(self, latency: Optional[int] = None, jitter: Optional[int] = None) -> None:
        self._dragging = False
        cfg = self.app.config
        lat = latency if latency is not None else self._snap(self._vars["latency"].get(), self.LATENCY_STEP)
        jit = jitter if jitter is not None else (
            self._snap(self._vars["jitter"].get(), self.JITTER_STEP) if "jitter" in self._vars else cfg.jitter_buffer_ms
        )
        lmin = self._snap(self._vars["lmin"].get(), self.LMIN_STEP) if "lmin" in self._vars else cfg.airplay_latency_min_ms
        if lat != cfg.receiver_latency_ms or jit != cfg.jitter_buffer_ms or lmin != cfg.airplay_latency_min_ms:
            cfg.receiver_latency_ms = lat
            cfg.jitter_buffer_ms = jit
            cfg.airplay_latency_min_ms = lmin
            self.app._apply_and_restart()  # pylint: disable=protected-access
        self._sync_from_config()


class QuickPanel(_PanelBase):
    """左鍵點圖示彈出的快捷面板（停留直到點別處）。"""

    def toggle(self) -> None:
        self.ui.call(self._toggle_visible)

    def hide(self) -> None:
        self.ui.call(self._hide)

    def _toggle_visible(self) -> None:
        if self._visible():
            self._hide()
        else:
            self._show()

    def _show(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self._refresh_all()
        # 開啟時以滑鼠游標（系統匣圖示）位置為錨點，之後刷新都固定在這裡，不跟著滑鼠跑
        self._anchor = (self.win.winfo_pointerx(), self.win.winfo_pointery())
        self._place()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self._schedule_tick()

    def _hide(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()

    def _place(self) -> None:
        """放在錨點（開啟時的系統匣圖示位置）左上方，夾在螢幕範圍內。"""
        win = self.win
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        px, py = getattr(self, "_anchor", None) or (win.winfo_pointerx(), win.winfo_pointery())
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        margin = self.ui.px(8)
        x = min(max(px - w, margin), sw - w - margin)
        y = min(max(py - h - margin, margin), sh - h - margin)
        win.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self) -> None:
        import tkinter as tk  # pylint: disable=import-outside-toplevel
        from tkinter import ttk  # pylint: disable=import-outside-toplevel

        win = tk.Toplevel(self.ui.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.win = win
        v = self._vars
        cfg = self.app.config

        border = tk.Frame(win, background="#9a9a9a")
        border.pack(fill="both", expand=True)
        outer = ttk.Frame(border, padding=12)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        outer.columnconfigure(0, weight=1)

        v["status"] = tk.StringVar()
        v["detail"] = tk.StringVar()
        ttk.Label(outer, textvariable=v["status"], style="Quick.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(outer, textvariable=v["detail"], style="Muted.TLabel", wraplength=self.ui.px(360), justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 8)
        )

        v["toggle_text"] = tk.StringVar(value="開始串流")
        btns = ttk.Frame(outer)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(btns, textvariable=v["toggle_text"], style="Big.TButton", command=self._toggle).pack(side="left")
        ttk.Button(btns, text="重新連線", command=lambda: _run_bg(self.app._reconnect)).pack(side="left", padx=(6, 0))  # pylint: disable=protected-access
        ttk.Button(btns, text="控制面板…", command=self._open_full).pack(side="right")

        ttk.Label(outer, text="接收端").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self._device_combo = ttk.Combobox(outer, state="readonly", width=40)
        self._device_combo.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        self._device_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_selected(self._device_combo))

        ttk.Label(outer, text="音量").grid(row=4, column=0, sticky="w", pady=(8, 0))
        volf = ttk.Frame(outer)
        volf.grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        volf.columnconfigure(0, weight=1)
        v["volume"] = tk.DoubleVar(value=float(cfg.volume if cfg.volume is not None else 50))
        v["volume_off"] = tk.BooleanVar(value=cfg.volume is None)
        v["volume_label"] = tk.StringVar()
        scale = ttk.Scale(volf, from_=0, to=100, variable=v["volume"], command=self._on_volume_move)
        scale.grid(row=0, column=0, sticky="ew")
        self._bind_drag(scale)
        scale.bind("<ButtonRelease-1>", lambda _e: self._on_volume_release())
        ttk.Label(volf, textvariable=v["volume_label"], width=5, anchor="e").grid(row=0, column=1, padx=(6, 0))
        ttk.Checkbutton(volf, text="不控制", variable=v["volume_off"], command=self._on_volume_off).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(outer, text="延遲").grid(row=5, column=0, sticky="w", pady=(8, 0))
        seg = ttk.Frame(outer)
        seg.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        v["quick_latency"] = tk.StringVar(value=latency_preset_name(cfg))
        for name in LATENCY_PRESETS:
            ttk.Radiobutton(
                seg,
                text=name.split("（")[0],
                value=name,
                variable=v["quick_latency"],
                style="Seg.Toolbutton",
                command=lambda n=name: self._apply_preset(n),
            ).pack(side="left", padx=(0, 4))

        v["update_text"] = tk.StringVar()
        upd = ttk.Frame(outer)
        upd.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(upd, textvariable=v["update_text"], style="Muted.TLabel").pack(side="left")
        self._update_btn = ttk.Button(upd, text="更新", command=lambda: _run_bg(self.app._update_now))  # pylint: disable=protected-access
        upd.grid_remove()
        self._update_row = upd

        win.bind("<Escape>", lambda _e: self._hide())
        win.bind("<FocusOut>", self._on_focus_out)

    def _open_full(self) -> None:
        self._hide()
        self.app.panel.show()

    def _on_focus_out(self, _event=None) -> None:
        # 下拉選單展開時焦點會暫時跑到 popdown；延遲後確認焦點真的離開本程式才收起
        def check():
            try:
                if self._visible() and self.win.focus_get() is None:
                    self._hide()
            except Exception:  # pylint: disable=broad-except
                pass

        self.win.after(150, check)

    def _refresh_all(self) -> None:
        if self.win is None:
            return
        self._fill_device_combo(self._device_combo)
        line, detail = self._status_lines()
        self._vars["status"].set(line)
        self._vars["detail"].set(detail)
        self._refresh_update_row()
        self._sync_from_config()

    def _refresh_update_row(self) -> None:
        text = self._update_text()
        self._vars["update_text"].set(text)
        if text:
            self._update_row.grid()
            self._update_btn.pack(side="right")
        else:
            self._update_row.grid_remove()

    def _schedule_tick(self) -> None:
        if self._tick_job is None:
            self._tick_job = self.win.after(1000, self._tick)

    def _tick(self) -> None:
        self._tick_job = None
        if not self._visible():
            return
        line, detail = self._status_lines()
        self._vars["status"].set(line)
        self._vars["detail"].set(detail)
        self._sync_from_config()
        # 內容高度可能改變（例如出現更新列），重新依錨點擺放，位置不變
        self._place()
        self._schedule_tick()


class ControlPanel(_PanelBase):
    """完整的控制面板視窗。"""

    def __init__(self, ui: UiThread, app: TrayApp) -> None:
        super().__init__(ui, app)
        self._capture_keys: list[Optional[int]] = []

    def _show(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self._refresh_all()
        self._schedule_tick()

    def _hide(self) -> None:
        if self.win is not None:
            self.win.withdraw()

    def _build(self) -> None:
        import tkinter as tk  # pylint: disable=import-outside-toplevel
        from tkinter import ttk  # pylint: disable=import-outside-toplevel

        win = tk.Toplevel(self.ui.root)
        win.title(f"{APP_NAME} 控制面板")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._hide)
        try:
            from PIL import ImageTk  # pylint: disable=import-outside-toplevel

            from .tray import make_icon_image  # pylint: disable=import-outside-toplevel

            self._icon_img = ImageTk.PhotoImage(make_icon_image(True, 64))
            win.iconphoto(True, self._icon_img)
        except Exception:  # pylint: disable=broad-except
            pass
        self.win = win
        v = self._vars
        cfg = self.app.config
        wrap = self.ui.px(470)

        outer = ttk.Frame(win, padding=14)
        outer.grid(sticky="nsew")

        # ---- 狀態列
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        v["status"] = tk.StringVar(value="閒置")
        v["detail"] = tk.StringVar(value="")
        ttk.Label(head, textvariable=v["status"], style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, textvariable=v["detail"], style="Muted.TLabel", wraplength=wrap, justify="left").grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )
        v["toggle_text"] = tk.StringVar(value="開始串流")
        ttk.Button(head, textvariable=v["toggle_text"], style="Big.TButton", command=self._toggle).grid(
            row=0, column=1, rowspan=2, sticky="e", padx=(12, 0)
        )

        ttk.Separator(outer).grid(row=1, column=0, sticky="ew", pady=10)

        # ---- 接收端
        rec = ttk.LabelFrame(outer, text="接收端", padding=(10, 6))
        rec.grid(row=2, column=0, sticky="ew")
        rec.columnconfigure(0, weight=1)
        self._device_combo = ttk.Combobox(rec, state="readonly", width=44)
        self._device_combo.grid(row=0, column=0, columnspan=3, sticky="ew")
        self._device_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_selected(self._device_combo))
        ttk.Button(rec, text="重新掃描", command=self._rescan).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(rec, text="重新連線", command=lambda: _run_bg(self.app._reconnect)).grid(  # pylint: disable=protected-access
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        self._pair_btn = ttk.Button(rec, text="配對…", command=self.app._pair_current)  # pylint: disable=protected-access
        self._pair_btn.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        self._pw_btn = ttk.Button(rec, text="設定密碼…", command=self.app._set_password_current)  # pylint: disable=protected-access
        self._pw_btn.grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Button(rec, text="新增 Snapcast…", command=self.app._add_snapcast).grid(  # pylint: disable=protected-access
            row=1, column=3, sticky="e", padx=(8, 0), pady=(6, 0)
        )

        # ---- 音量
        volf = ttk.LabelFrame(outer, text="接收端音量", padding=(10, 6))
        volf.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        volf.columnconfigure(0, weight=1)
        v["volume"] = tk.DoubleVar(value=float(cfg.volume if cfg.volume is not None else 50))
        v["volume_label"] = tk.StringVar()
        v["volume_off"] = tk.BooleanVar(value=cfg.volume is None)
        scale = ttk.Scale(volf, from_=0, to=100, variable=v["volume"], command=self._on_volume_move)
        scale.grid(row=0, column=0, sticky="ew")
        self._bind_drag(scale)
        scale.bind("<ButtonRelease-1>", lambda _e: self._on_volume_release())
        ttk.Label(volf, textvariable=v["volume_label"], width=6, anchor="e").grid(row=0, column=1, padx=(8, 0))
        ttk.Checkbutton(volf, text="不控制接收端音量", variable=v["volume_off"], command=self._on_volume_off).grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )

        # ---- 延遲
        latf = ttk.LabelFrame(outer, text="延遲（放開滑桿後套用，會重新連線）", padding=(10, 6))
        latf.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        latf.columnconfigure(1, weight=1)
        v["preset"] = tk.StringVar(value=latency_preset_name(cfg))
        ttk.Label(latf, text="預設組").grid(row=0, column=0, sticky="w")
        pc = ttk.Combobox(latf, textvariable=v["preset"], state="readonly", values=list(LATENCY_PRESETS) + ["自訂"], width=22)
        pc.grid(row=0, column=1, sticky="w", padx=8)
        pc.bind("<<ComboboxSelected>>", self._on_preset_selected)
        v["latency"] = tk.DoubleVar(value=float(cfg.receiver_latency_ms))
        v["latency_label"] = tk.StringVar()
        ttk.Label(latf, text="接收端緩衝").grid(row=1, column=0, sticky="w", pady=(4, 0))
        s = ttk.Scale(latf, from_=self.LATENCY_MIN, to=self.LATENCY_MAX, variable=v["latency"], command=lambda _v: self._update_labels())
        s.grid(row=1, column=1, sticky="ew", padx=8, pady=(4, 0))
        self._bind_drag(s)
        s.bind("<ButtonRelease-1>", lambda _e: self._apply_latency())
        ttk.Label(latf, textvariable=v["latency_label"], width=8, anchor="e").grid(row=1, column=2, pady=(4, 0))
        v["jitter"] = tk.DoubleVar(value=float(cfg.jitter_buffer_ms))
        v["jitter_label"] = tk.StringVar()
        ttk.Label(latf, text="本機抖動緩衝").grid(row=2, column=0, sticky="w", pady=(4, 0))
        s2 = ttk.Scale(latf, from_=self.JITTER_MIN, to=self.JITTER_MAX, variable=v["jitter"], command=lambda _v: self._update_labels())
        s2.grid(row=2, column=1, sticky="ew", padx=8, pady=(4, 0))
        self._bind_drag(s2)
        s2.bind("<ButtonRelease-1>", lambda _e: self._apply_latency())
        ttk.Label(latf, textvariable=v["jitter_label"], width=8, anchor="e").grid(row=2, column=2, pady=(4, 0))
        v["lmin"] = tk.DoubleVar(value=float(cfg.airplay_latency_min_ms))
        v["lmin_label"] = tk.StringVar()
        ttk.Label(latf, text="HomePod 最小緩衝").grid(row=3, column=0, sticky="w", pady=(4, 0))
        s3 = ttk.Scale(latf, from_=self.LMIN_MIN, to=self.LMIN_MAX, variable=v["lmin"], command=lambda _v: self._update_labels())
        s3.grid(row=3, column=1, sticky="ew", padx=8, pady=(4, 0))
        self._bind_drag(s3)
        s3.bind("<ButtonRelease-1>", lambda _e: self._apply_latency())
        ttk.Label(latf, textvariable=v["lmin_label"], width=8, anchor="e").grid(row=3, column=2, pady=(4, 0))
        v["latency_hint"] = tk.StringVar()
        ttk.Label(latf, textvariable=v["latency_hint"], style="Muted.TLabel", wraplength=wrap, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )

        # ---- 音質與來源
        qf = ttk.LabelFrame(outer, text="音質與擷取來源", padding=(10, 6))
        qf.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        qf.columnconfigure(1, weight=1)
        ttk.Label(qf, text="時鐘漂移補償").grid(row=0, column=0, sticky="w")
        v["drift"] = tk.StringVar(value=cfg.drift_compensation)
        dr = ttk.Frame(qf)
        dr.grid(row=0, column=1, columnspan=2, sticky="w", padx=8)
        for label, mode in DRIFT_MODES.items():
            ttk.Radiobutton(dr, text=label, value=mode, variable=v["drift"], command=self._on_drift).pack(side="left", padx=(0, 10))
        ttk.Label(qf, text="重取樣品質").grid(row=1, column=0, sticky="w", pady=(4, 0))
        v["quality"] = tk.StringVar()
        qc = ttk.Combobox(qf, textvariable=v["quality"], state="readonly", values=list(RESAMPLE_QUALITIES), width=14)
        qc.grid(row=1, column=1, sticky="w", padx=8, pady=(4, 0))
        qc.bind("<<ComboboxSelected>>", lambda _e: self.app._set_quality(RESAMPLE_QUALITIES[v["quality"].get()]))  # pylint: disable=protected-access
        v["dither"] = tk.BooleanVar(value=cfg.dither)
        ttk.Checkbutton(qf, text="TPDF dither（轉 16-bit 時）", variable=v["dither"], command=self._on_dither).grid(
            row=1, column=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(qf, text="擷取來源").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._capture_combo = ttk.Combobox(qf, state="readonly", width=40)
        self._capture_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=(4, 0))
        self._capture_combo.bind("<<ComboboxSelected>>", self._on_capture_selected)

        # ---- 更新
        upf = ttk.LabelFrame(outer, text="更新", padding=(10, 6))
        upf.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        upf.columnconfigure(0, weight=1)
        v["update_text"] = tk.StringVar()
        ttk.Label(upf, textvariable=v["update_text"], wraplength=wrap, justify="left").grid(row=0, column=0, sticky="w")
        self._update_btn = ttk.Button(upf, text="更新", command=lambda: _run_bg(self.app._update_now))  # pylint: disable=protected-access
        self._update_btn.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(upf, text="立即檢查", command=lambda: _run_bg(self.app._check_updates_now)).grid(  # pylint: disable=protected-access
            row=0, column=2, padx=(6, 0)
        )
        v["update_days"] = tk.IntVar(value=cfg.update_check_days)
        daysf = ttk.Frame(upf)
        daysf.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(daysf, text="每").pack(side="left")
        sp = ttk.Spinbox(daysf, from_=0, to=90, width=4, textvariable=v["update_days"], command=self._on_update_days)
        sp.pack(side="left", padx=4)
        sp.bind("<FocusOut>", lambda _e: self._on_update_days())
        ttk.Label(daysf, text="天自動檢查（0 = 不檢查，預設關閉）").pack(side="left")

        # ---- 選項
        opt = ttk.Frame(outer)
        opt.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        v["auto_stream"] = tk.BooleanVar(value=cfg.autostart_stream)
        v["auto_win"] = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(opt, text="啟動後自動連線到上次裝置", variable=v["auto_stream"], command=self._on_auto_stream).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(opt, text="登入 Windows 時自動啟動", variable=v["auto_win"], command=self._on_auto_win).grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )
        btns = ttk.Frame(outer)
        btns.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="允許通過 Windows 防火牆", command=self.app._add_firewall_rules).pack(side="left")  # pylint: disable=protected-access
        ttk.Button(btns, text="開啟記錄資料夾", command=self.app._open_logs).pack(side="left", padx=(6, 0))  # pylint: disable=protected-access
        ttk.Button(btns, text="結束 AirSender", command=self.app._quit).pack(side="left", padx=(6, 0))  # pylint: disable=protected-access
        ttk.Label(btns, text=f"{APP_NAME} v{__version__}", style="Muted.TLabel").pack(side="right")

    # ------------------------------------------------------------ 更新
    def _refresh_all(self) -> None:
        if self.win is None:
            return
        self._fill_device_combo(self._device_combo)
        self._refresh_capture()
        self._refresh_status()
        self._sync_from_config()

    def _refresh_capture(self) -> None:
        names = ["跟隨 Windows 預設輸出"]
        keys: list[Optional[int]] = [None]
        for index, name, rate in self.app.capture_devices:
            names.append(f"{name}（{rate} Hz）")
            keys.append(index)
        self._capture_keys = keys
        self._capture_combo["values"] = names
        idx = keys.index(self.app.config.capture_device_index) if self.app.config.capture_device_index in keys else 0
        self._capture_combo.current(idx)

    def _refresh_status(self) -> None:
        line, detail = self._status_lines()
        self._vars["status"].set(line)
        self._vars["detail"].set(detail)
        device = self.app.streamer.device or self.app._current_device()  # pylint: disable=protected-access
        is_airplay = device is not None and device.kind == "airplay"
        self._pair_btn.state(["!disabled"] if is_airplay else ["disabled"])
        self._pw_btn.state(["!disabled"] if is_airplay else ["disabled"])
        if device is None:
            hint = "請先選擇接收端。"
        elif is_airplay:
            cfg = self.app.config
            est = cfg.airplay_latency_min_ms + cfg.receiver_latency_ms + cfg.jitter_buffer_ms + 15
            hint = (
                f"AirPlay 總延遲 ≈ HomePod 最小緩衝 + 接收端緩衝 + 抖動緩衝 + 約 15 ms ≈ {est} ms。"
                "HomePod mini 實測與此公式一致，地板約 140 ms（接收端緩衝低於 100 ms 不再更低且會重播）。"
                "若斷音或聲音重複，先調高接收端緩衝。"
            )
        else:
            hint = "此種類接收端自行決定緩衝（2–5 秒），延遲設定對它無效。"
        self._vars["latency_hint"].set(hint)
        from .updater import describe_last_check  # pylint: disable=import-outside-toplevel

        busy = self.app._update_busy  # pylint: disable=protected-access
        if busy:
            text = busy
        elif self._update_text():
            text = self._update_text()
        elif self.app.updater.last_error:
            text = f"{self.app.updater.last_error}（上次檢查：{describe_last_check(self.app.config)}）"
        else:
            text = f"目前 v{__version__} 已是最新（上次檢查：{describe_last_check(self.app.config)}）"
        self._vars["update_text"].set(text)
        enabled = self.app.updater.available is not None and not busy
        self._update_btn.state(["!disabled"] if enabled else ["disabled"])

    def _on_update_days(self) -> None:
        try:
            days = int(self._vars["update_days"].get())
        except Exception:  # pylint: disable=broad-except
            return
        self.app.config.update_check_days = max(0, min(365, days))
        self.app.config.save()

    def _schedule_tick(self) -> None:
        if self._tick_job is None:
            self._tick_job = self.win.after(1000, self._tick)

    def _tick(self) -> None:
        self._tick_job = None
        if self.win is None or not self.win.winfo_exists():
            return
        if self.win.state() != "withdrawn":
            self._refresh_status()
            self._sync_from_config()
            self._refresh_capture_if_changed()
        self._schedule_tick()

    def _refresh_capture_if_changed(self) -> None:
        wanted = self.app.config.capture_device_index
        idx = self._capture_combo.current()
        current = self._capture_keys[idx] if 0 <= idx < len(self._capture_keys) else None
        if current != wanted and wanted in self._capture_keys:
            self._capture_combo.current(self._capture_keys.index(wanted))

    # ------------------------------------------------------------ 事件
    def _rescan(self) -> None:
        self.app._rescan_async()  # pylint: disable=protected-access
        self._fill_device_combo(self._device_combo)

    def _on_preset_selected(self, _event=None) -> None:
        name = self._vars["preset"].get()
        if name in LATENCY_PRESETS:
            self._apply_preset(name)

    def _on_drift(self) -> None:
        self.app._set_drift_mode(self._vars["drift"].get())  # pylint: disable=protected-access

    def _on_dither(self) -> None:
        self.app.config.dither = self._vars["dither"].get()
        self.app._apply_and_restart()  # pylint: disable=protected-access

    def _on_capture_selected(self, _event=None) -> None:
        idx = self._capture_combo.current()
        if 0 <= idx < len(self._capture_keys):
            self.app._set_capture_device(self._capture_keys[idx])  # pylint: disable=protected-access

    def _on_auto_stream(self) -> None:
        self.app.config.autostart_stream = self._vars["auto_stream"].get()
        self.app.config.save()

    def _on_auto_win(self) -> None:
        autostart.set_enabled(self._vars["auto_win"].get())
