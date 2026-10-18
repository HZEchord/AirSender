"""Windows 防火牆規則。

AirPlay 2 的 SETUP 流程需要接收端反向連回電腦（事件通道）並以 UDP 回傳時間同步與控制封包。
若防火牆攔下入站連線，症狀是「no response to SETUP / pair-pin-start」逾時。
安裝程式會以系統管理員身分加入規則；這裡提供程式內的檢查與（經 UAC 提權的）補救。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import List

from . import APP_NAME

_LOGGER = logging.getLogger(__name__)
RULE_NAME = APP_NAME


def programs() -> List[str]:
    """需要放行的執行檔。"""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        paths = [exe]
        cli = exe.with_name("AirSenderCLI.exe")
        tray = exe.with_name("AirSender.exe")
        for other in (cli, tray):
            if other.exists() and other not in paths:
                paths.append(other)
        return [str(p) for p in paths]
    return [str(exe)]


def rules_present() -> bool:
    """是否已有放行目前執行檔的入站規則（不需管理員權限即可查詢）。

    從原始碼執行時（python.exe）通常已被使用者在第一次提示時放行，且規則名稱不固定，
    這裡不檢查以免誤報。
    """
    if not getattr(sys, "frozen", False):
        return True
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE_NAME}", "verbose"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.debug("查詢防火牆規則失敗：%s", exc)
        return True  # 查不到就不要誤報
    if result.returncode != 0:
        return False
    out = result.stdout.lower()
    return any(p.lower() in out for p in programs())


def add_rules() -> bool:
    """以 UAC 提權執行 netsh 加入入站允許規則。回傳是否成功啟動提權程序。"""
    cmds = [f'netsh advfirewall firewall delete rule name="{RULE_NAME}"']
    for program in programs():
        cmds.append(
            f'netsh advfirewall firewall add rule name="{RULE_NAME}" dir=in action=allow '
            f'program="{program}" enable=yes profile=any'
        )
    params = "/c " + " & ".join(cmds)
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        ret = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", "cmd.exe", params, None, 0
        )
        ok = ret > 32
        if not ok:
            _LOGGER.warning("提權執行 netsh 失敗（代碼 %s），可能是使用者取消", ret)
        return ok
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("無法加入防火牆規則：%s", exc)
        return False
