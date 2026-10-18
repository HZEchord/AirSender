<#
.SYNOPSIS
  建置 AirSender：PyInstaller 打包 + Inno Setup 安裝程式。

.EXAMPLE
  .\build.ps1                 # 完整建置，產生 dist\installer\AirSender-Setup-x.y.z.exe
  .\build.ps1 -SkipInstaller  # 只做 PyInstaller（dist\AirSender\AirSender.exe）
#>
param(
    [switch]$SkipInstaller
)

# 原生程式（pip / PyInstaller）會把一般日誌寫到 stderr，用 Continue 避免被當成錯誤；改以 $LASTEXITCODE 判斷
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "建立虛擬環境 .venv ..."
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { py -3 -m venv .venv } else { python -m venv .venv }
}
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "安裝相依套件 ..."
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip 安裝失敗" }

Write-Host "產生圖示 ..."
& $python tools\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "產生圖示失敗" }

Write-Host "PyInstaller 打包 ..."
& $python -m PyInstaller --noconfirm --clean --log-level WARN AirSender.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失敗" }
Write-Host "已產生 dist\AirSender\AirSender.exe"

if ($SkipInstaller) { exit 0 }

$candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
)
$iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    Write-Warning "找不到 Inno Setup 6（ISCC.exe）。請安裝：winget install -e --id JRSoftware.InnoSetup"
    exit 1
}

Write-Host "Inno Setup 建置安裝程式 ..."
& $iscc "installer\AirSender.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 失敗" }
Get-ChildItem "dist\installer\*.exe" | ForEach-Object { Write-Host "完成：$($_.FullName)" }
