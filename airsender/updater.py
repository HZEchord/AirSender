"""自動更新：定期向 GitHub Releases 查詢新版本，下載安裝程式、驗證 SHA-256 後啟動安裝。

- 預設每 10 天查一次（config.update_check_days），程式啟動 1 分鐘後也會依間隔決定是否查詢
- 只查詢，不自動安裝：發現新版會通知，由使用者按「更新」才下載與安裝
- 私有倉庫測試時可設環境變數 AIRSENDER_GITHUB_TOKEN；正式發布請把倉庫設為公開
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import APP_NAME, __version__
from .config import Config

_LOGGER = logging.getLogger(__name__)

REPO = "HZEchord/AirSender"
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
ASSET_PATTERN = re.compile(r"^AirSender-Setup-[A-Za-z0-9._-]+\.exe$", re.IGNORECASE)  # 不允許路徑字元
ALLOWED_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
USER_AGENT = f"{APP_NAME}/{__version__}"


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    page_url: str
    notes: str
    asset_name: str
    asset_url: str
    asset_api_url: str  # API 端點（私有倉庫需帶 token 並以 octet-stream 取得）
    asset_size: int
    digest: Optional[str]  # "sha256:<hex>"


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.4.1' / '0.4.1-beta' -> (0, 4, 1)。無法解析的部分視為 0。"""
    nums = re.findall(r"\d+", text.split("-")[0])
    return tuple(int(n) for n in nums[:3]) or (0,)


def fetch_latest(repo: str = REPO, timeout: float = 10.0) -> Optional[Release]:
    """回傳最新 Release；沒有 Release、倉庫不可讀或網路失敗時回傳 None。"""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.environ.get("AIRSENDER_GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API_LATEST.format(repo=repo), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _LOGGER.info("查詢更新：HTTP %s（倉庫可能是私有或尚無 Release）", exc.code)
        return None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        _LOGGER.info("查詢更新失敗：%s", exc)
        return None

    asset = next((a for a in data.get("assets", []) if ASSET_PATTERN.match(a.get("name", ""))), None)
    if asset is None:
        _LOGGER.info("最新 Release %s 沒有安裝程式附件", data.get("tag_name"))
        return None
    tag = str(data.get("tag_name", ""))
    return Release(
        version=".".join(str(n) for n in parse_version(tag)),
        tag=tag,
        page_url=str(data.get("html_url", "")),
        notes=str(data.get("body") or ""),
        asset_name=str(asset["name"]),
        asset_url=str(asset["browser_download_url"]),
        asset_api_url=str(asset.get("url") or ""),
        asset_size=int(asset.get("size") or 0),
        digest=asset.get("digest"),
    )


def is_newer(release: Release, current: str = __version__) -> bool:
    return parse_version(release.version) > parse_version(current)


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"拒絕非 GitHub 的下載位址：{url[:80]}")


def expected_sha256(release: Release) -> str:
    """Release 必須帶 sha256 digest，否則拒絕安裝（fail closed）。"""
    algo, _, expected = (release.digest or "").partition(":")
    if algo.lower() != "sha256" or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise RuntimeError(
            "GitHub 沒有提供這個安裝程式的 SHA-256，為安全起見不自動安裝；請到 Release 頁面手動下載"
        )
    return expected.lower()


def sha256_of(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def download(release: Release, progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """下載安裝程式到新建的私有暫存資料夾並驗證 SHA-256；沒有 digest 一律拒絕。"""
    expected = expected_sha256(release)
    if not ASSET_PATTERN.match(release.asset_name) or Path(release.asset_name).name != release.asset_name:
        raise RuntimeError("安裝程式檔名不合法")
    headers = {"User-Agent": USER_AGENT}
    url = release.asset_url
    token = os.environ.get("AIRSENDER_GITHUB_TOKEN")
    if token and release.asset_api_url:
        url = release.asset_api_url
        if urllib.parse.urlparse(url).hostname != "api.github.com":
            raise RuntimeError("拒絕把 token 送到非 api.github.com 的位址")
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/octet-stream"
    _check_url(url)
    dest_dir = Path(tempfile.mkdtemp(prefix="AirSender-update-"))  # 每次新建，權限只有本使用者
    dest = dest_dir / release.asset_name
    req = urllib.request.Request(url, headers=headers)
    sha = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.geturl() and urllib.parse.urlparse(resp.geturl()).hostname not in ALLOWED_HOSTS:
            raise RuntimeError("下載被轉向到非 GitHub 的位址")
        total = int(resp.headers.get("Content-Length") or release.asset_size or 0)
        if total > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("安裝程式大小異常，拒絕下載")
        with open(dest, "xb") as out:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("安裝程式大小異常，中止下載")
                out.write(chunk)
                sha.update(chunk)
                if progress:
                    progress(done, total)
    if sha.hexdigest().lower() != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError("下載的安裝程式 SHA-256 與 GitHub 記錄不符，已刪除")
    _LOGGER.info("已下載 %s（%d bytes，SHA-256 相符）", dest, done)
    return dest


def launch_installer(path: Path, expected_sha256_hex: Optional[str] = None) -> bool:
    """以 Inno Setup 靜默模式啟動安裝程式（會跳 UAC）；安裝完成後安裝程式會重新啟動 AirSender。

    啟動前再算一次 SHA-256，縮小「下載後、執行前」檔案被同一使用者的其他程式掉包的空窗。
    """
    try:
        if expected_sha256_hex and sha256_of(path).lower() != expected_sha256_hex.lower():
            _LOGGER.error("安裝程式在啟動前被修改，拒絕執行：%s", path)
            path.unlink(missing_ok=True)
            return False
        import ctypes  # pylint: disable=import-outside-toplevel

        ret = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "open", str(path), "/SILENT /CLOSEAPPLICATIONS /NORESTART", None, 1
        )
        return ret > 32
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("無法啟動安裝程式：%s", exc)
        return False


class UpdateChecker:
    """依設定的天數定期查詢；發現新版時呼叫 on_available。"""

    def __init__(self, config: Config, on_available: Callable[[Release], None]) -> None:
        self.config = config
        self.on_available = on_available
        self.available: Optional[Release] = None
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="update-check", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def due(self) -> bool:
        days = max(1, int(self.config.update_check_days or 10))
        last = self.config.last_update_check
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - last_dt).total_seconds() >= days * 86400

    def check(self, force: bool = False) -> Optional[Release]:
        """立即查詢。回傳比目前新的 Release（沒有則 None）。"""
        if not force and not self.due():
            return self.available
        release = fetch_latest()
        self.config.last_update_check = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.config.save()
        if release is None:
            self.last_error = "無法取得版本資訊"
            return None
        self.last_error = ""
        if is_newer(release) and release.version != self.config.update_skip_version:
            self.available = release
            try:
                self.on_available(release)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("更新通知失敗")
            return release
        self.available = None
        return None

    def _loop(self) -> None:
        # 啟動 60 秒後第一次檢查（避免拖慢啟動），之後每 6 小時看一次是否到期
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.debug("定期更新檢查失敗：%s", exc)
            if self._stop.wait(6 * 3600):
                return


def describe_last_check(config: Config) -> str:
    if not config.last_update_check:
        return "尚未檢查"
    try:
        dt = datetime.fromisoformat(config.last_update_check).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return config.last_update_check
