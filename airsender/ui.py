"""控制面板（tkinter）與 UI 執行緒。

pystray 佔用主執行緒跑 Win32 訊息迴圈，tkinter 則在自己的執行緒擁有一個隱藏的根視窗；
所有 Tk 操作都經由 UiThread.call() 送到該執行緒執行（Tk 不是執行緒安全的）。
控制面板是左鍵點系統匣圖示開啟的視窗：狀態、接收端選擇、音量 / 延遲滑桿、音質與擷取設定。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Optional

from . import APP_NAME, __version__, autostart
from .config import DRIFT_MODES, RESAMPLE_QUALITIES
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
        style.configure("Muted.TLabel", foreground="#666666")
        style.configure("Big.TButton", font=(UI_FONT, 11, "bold"), padding=(16, 6))
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


class ControlPanel:
    """左鍵點圖示開啟的控制面板。所有方法除 show()/refresh() 外都在 UI 執行緒執行。"""

    LATENCY_MIN, LATENCY_MAX, LATENCY_STEP = 100, 2000, 50
    JITTER_MIN, JITTER_MAX, JITTER_STEP = 10, 150, 5

    def __init__(self, ui: UiThread, app: TrayApp) -> None:
        self.ui = ui
        self.app = app
        self.win: Any = None
        self._vars: dict[str, Any] = {}
        self._device_keys: list[str] = []
        self._capture_keys: list[Optional[int]] = []
        self._volume_job: Any = None

    # ------------------------------------------------------------ 執行緒安全入口
    def show(self) -> None:
        self.ui.call(self._show)

    def refresh(self) -> None:
        if self.win is not None:
            self.ui.call(self._refresh_all)

    # ------------------------------------------------------------ 建立
    def _show(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self._refresh_all()

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
        # Tk 的 wraplength 以實體像素計；依 DPI 縮放（tk scaling = 每點像素數，96 DPI 時為 4/3）
        scale = float(self.ui.root.tk.call("tk", "scaling")) / (96 / 72)
        wrap = int(470 * scale)

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
        self._toggle_btn = ttk.Button(head, textvariable=v["toggle_text"], style="Big.TButton", command=self._toggle)
        self._toggle_btn.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        ttk.Separator(outer).grid(row=1, column=0, sticky="ew", pady=10)

        # ---- 接收端
        rec = ttk.LabelFrame(outer, text="接收端", padding=(10, 6))
        rec.grid(row=2, column=0, sticky="ew")
        rec.columnconfigure(0, weight=1)
        v["device"] = tk.StringVar()
        self._device_combo = ttk.Combobox(rec, textvariable=v["device"], state="readonly", width=44)
        self._device_combo.grid(row=0, column=0, columnspan=3, sticky="ew")
        self._device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        ttk.Button(rec, text="重新掃描", command=self._rescan).grid(row=0, column=3, padx=(8, 0))
        self._pair_btn = ttk.Button(rec, text="配對…", command=self.app._pair_current)
        self._pair_btn.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._pw_btn = ttk.Button(rec, text="設定密碼…", command=self.app._set_password_current)
        self._pw_btn.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Button(rec, text="新增 Snapcast…", command=self.app._add_snapcast).grid(
            row=1, column=2, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        # ---- 音量
        volf = ttk.LabelFrame(outer, text="接收端音量", padding=(10, 6))
        volf.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        volf.columnconfigure(0, weight=1)
        v["volume"] = tk.DoubleVar(value=float(cfg.volume if cfg.volume is not None else 50))
        v["volume_label"] = tk.StringVar()
        v["volume_off"] = tk.BooleanVar(value=cfg.volume is None)
        self._vol_scale = ttk.Scale(volf, from_=0, to=100, variable=v["volume"], command=self._on_volume_move)
        self._vol_scale.grid(row=0, column=0, sticky="ew")
        self._vol_scale.bind("<ButtonRelease-1>", lambda _e: self._apply_volume())
        ttk.Label(volf, textvariable=v["volume_label"], width=6, anchor="e").grid(row=0, column=1, padx=(8, 0))
        ttk.Checkbutton(volf, text="不控制接收端音量", variable=v["volume_off"], command=self._on_volume_off).grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )

        # ---- 延遲
        latf = ttk.LabelFrame(outer, text="延遲（放開滑桿後套用，會重新連線）", padding=(10, 6))
        latf.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        latf.columnconfigure(1, weight=1)
        v["latency"] = tk.DoubleVar(value=float(cfg.receiver_latency_ms))
        v["latency_label"] = tk.StringVar()
        ttk.Label(latf, text="接收端緩衝").grid(row=0, column=0, sticky="w")
        s = ttk.Scale(latf, from_=self.LATENCY_MIN, to=self.LATENCY_MAX, variable=v["latency"], command=self._on_latency_move)
        s.grid(row=0, column=1, sticky="ew", padx=8)
        s.bind("<ButtonRelease-1>", lambda _e: self._apply_latency())
        ttk.Label(latf, textvariable=v["latency_label"], width=8, anchor="e").grid(row=0, column=2)
        v["jitter"] = tk.DoubleVar(value=float(cfg.jitter_buffer_ms))
        v["jitter_label"] = tk.StringVar()
        ttk.Label(latf, text="本機抖動緩衝").grid(row=1, column=0, sticky="w", pady=(4, 0))
        s2 = ttk.Scale(latf, from_=self.JITTER_MIN, to=self.JITTER_MAX, variable=v["jitter"], command=self._on_jitter_move)
        s2.grid(row=1, column=1, sticky="ew", padx=8, pady=(4, 0))
        s2.bind("<ButtonRelease-1>", lambda _e: self._apply_latency())
        ttk.Label(latf, textvariable=v["jitter_label"], width=8, anchor="e").grid(row=1, column=2, pady=(4, 0))
        v["latency_hint"] = tk.StringVar()
        ttk.Label(latf, textvariable=v["latency_hint"], style="Muted.TLabel", wraplength=wrap, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )

        # ---- 音質與來源
        qf = ttk.LabelFrame(outer, text="音質與擷取來源", padding=(10, 6))
        qf.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        qf.columnconfigure(1, weight=1)
        ttk.Label(qf, text="時鐘漂移補償").grid(row=0, column=0, sticky="w")
        v["drift"] = tk.StringVar(value=cfg.drift_compensation)
        dr = ttk.Frame(qf)
        dr.grid(row=0, column=1, sticky="w", padx=8)
        for label, mode in DRIFT_MODES.items():
            ttk.Radiobutton(dr, text=label, value=mode, variable=v["drift"], command=self._on_drift).pack(side="left", padx=(0, 10))
        ttk.Label(qf, text="重取樣品質").grid(row=1, column=0, sticky="w", pady=(4, 0))
        v["quality"] = tk.StringVar(value=next((k for k, q in RESAMPLE_QUALITIES.items() if q == cfg.resample_quality), "最高 (VHQ)"))
        qc = ttk.Combobox(qf, textvariable=v["quality"], state="readonly", values=list(RESAMPLE_QUALITIES), width=14)
        qc.grid(row=1, column=1, sticky="w", padx=8, pady=(4, 0))
        qc.bind("<<ComboboxSelected>>", lambda _e: self.app._set_quality(RESAMPLE_QUALITIES[v["quality"].get()]))
        v["dither"] = tk.BooleanVar(value=cfg.dither)
        ttk.Checkbutton(qf, text="TPDF dither（轉 16-bit 時）", variable=v["dither"], command=self._on_dither).grid(
            row=1, column=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(qf, text="擷取來源").grid(row=2, column=0, sticky="w", pady=(4, 0))
        v["capture"] = tk.StringVar()
        self._capture_combo = ttk.Combobox(qf, textvariable=v["capture"], state="readonly", width=40)
        self._capture_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=(4, 0))
        self._capture_combo.bind("<<ComboboxSelected>>", self._on_capture_selected)

        # ---- 選項
        opt = ttk.Frame(outer)
        opt.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        v["auto_stream"] = tk.BooleanVar(value=cfg.autostart_stream)
        v["auto_win"] = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(opt, text="啟動後自動連線到上次裝置", variable=v["auto_stream"], command=self._on_auto_stream).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(opt, text="登入 Windows 時自動啟動", variable=v["auto_win"], command=self._on_auto_win).grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )
        btns = ttk.Frame(outer)
        btns.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="允許通過 Windows 防火牆", command=self.app._add_firewall_rules).pack(side="left")
        ttk.Button(btns, text="開啟記錄資料夾", command=self.app._open_logs).pack(side="left", padx=(6, 0))
        ttk.Label(btns, text=f"{APP_NAME} v{__version__}", style="Muted.TLabel").pack(side="right")

        self._refresh_devices()
        self._refresh_capture()
        self._update_labels()
        self._tick()

    # ------------------------------------------------------------ 更新
    def _refresh_all(self) -> None:
        if self.win is None:
            return
        self._refresh_devices()
        self._refresh_capture()
        self._refresh_status()

    def _refresh_devices(self) -> None:
        names = []
        keys = []
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
        self._device_keys = keys
        self._device_combo["values"] = names
        current = self.app.streamer.device or self.app._current_device()  # pylint: disable=protected-access
        if current and current.identifier in keys:
            self._device_combo.current(keys.index(current.identifier))
        elif names:
            self._device_combo.current(0)

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
        st = self.app.streamer
        device = st.device or self.app._current_device()  # pylint: disable=protected-access
        v = self._vars
        line = st.state.value
        if device:
            line += f" → {device.name}（{KIND_LABELS.get(device.kind, device.kind)}）"
        v["status"].set(line)
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
        v["detail"].set("\n".join(parts))
        v["toggle_text"].set("停止串流" if st.is_active else "開始串流")
        is_airplay = device is not None and device.kind == "airplay"
        self._pair_btn.state(["!disabled"] if is_airplay else ["disabled"])
        self._pw_btn.state(["!disabled"] if is_airplay else ["disabled"])
        if device is None:
            hint = "請先選擇接收端。"
        elif is_airplay:
            hint = (
                "AirPlay：接收端緩衝是總延遲的主要來源。HomePod mini 實測總延遲：150 ms → 約 0.44 s、"
                "250 ms → 約 0.52 s（HomePod 內部固定約 0.25 s）；100 ms 會斷音。斷音就調高一級。"
            )
        else:
            hint = "此種類接收端自行決定緩衝（2–5 秒），延遲設定對它無效。"
        v["latency_hint"].set(hint)

    def _update_labels(self) -> None:
        v = self._vars
        v["volume_label"].set(f"{int(round(v['volume'].get()))}%")
        v["latency_label"].set(f"{self._snap(v['latency'].get(), self.LATENCY_STEP)} ms")
        v["jitter_label"].set(f"{self._snap(v['jitter'].get(), self.JITTER_STEP)} ms")

    def _tick(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        if self.win.state() != "withdrawn":
            self._refresh_status()
        self.win.after(1000, self._tick)

    @staticmethod
    def _snap(value: float, step: int) -> int:
        return int(round(value / step) * step)

    # ------------------------------------------------------------ 事件
    def _run_bg(self, fn: Callable, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _toggle(self) -> None:
        self._run_bg(self.app._toggle_stream)  # pylint: disable=protected-access

    def _rescan(self) -> None:
        self.app._rescan_async()  # pylint: disable=protected-access
        self._refresh_devices()

    def _on_device_selected(self, _event=None) -> None:
        idx = self._device_combo.current()
        if idx < 0 or idx >= len(self._device_keys) or not self._device_keys[idx]:
            return
        key = self._device_keys[idx]
        device = next((d for d in self.app.devices if d.identifier == key), None)
        if device is not None:
            self.app._select_device(device)  # pylint: disable=protected-access

    def _on_volume_move(self, _value=None) -> None:
        self._update_labels()
        if self._vars["volume_off"].get():
            self._vars["volume_off"].set(False)
        # 拖曳中每 150 ms 套用一次，放開時再套用一次
        if self._volume_job is None:
            self._volume_job = self.win.after(150, self._apply_volume)

    def _apply_volume(self) -> None:
        self._volume_job = None
        if self._vars["volume_off"].get():
            return
        pct = int(round(self._vars["volume"].get()))
        self._run_bg(self.app.streamer.set_volume, pct)

    def _on_volume_off(self) -> None:
        if self._vars["volume_off"].get():
            self.app._set_volume(None)  # pylint: disable=protected-access
        else:
            self._apply_volume()

    def _on_latency_move(self, _value=None) -> None:
        self._update_labels()

    def _on_jitter_move(self, _value=None) -> None:
        self._update_labels()

    def _apply_latency(self) -> None:
        lat = self._snap(self._vars["latency"].get(), self.LATENCY_STEP)
        jit = self._snap(self._vars["jitter"].get(), self.JITTER_STEP)
        self._vars["latency"].set(lat)
        self._vars["jitter"].set(jit)
        self._update_labels()
        cfg = self.app.config
        if lat != cfg.receiver_latency_ms or jit != cfg.jitter_buffer_ms:
            cfg.receiver_latency_ms = lat
            cfg.jitter_buffer_ms = jit
            self.app._apply_and_restart()  # pylint: disable=protected-access

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
