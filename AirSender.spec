# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 設定：產生 dist/AirSender/（AirSender.exe 系統匣版 + AirSenderCLI.exe 主控台版）。"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (
    collect_submodules("pyatv")
    + collect_submodules("zeroconf")
    + [
        "pystray._win32",
        "PIL.ImageDraw",
        "soxr",
        "pyaudiowpatch",
        "miniaudio",
        "numpy",
    ]
)
datas = collect_data_files("pyatv") + collect_data_files("miniaudio") + collect_data_files("soxr")

a = Analysis(
    ["run_airsender.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # tkinter 用於 PIN / 密碼輸入對話框，需保留
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_tray = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AirSender",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/airsender.ico",
)
exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AirSenderCLI",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon="assets/airsender.ico",
)
coll = COLLECT(
    exe_tray,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AirSender",
)
