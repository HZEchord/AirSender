# 提交前檢查：與 CI 相同的 lint / 格式 / 單元測試。用法：.\check.ps1
$ErrorActionPreference = "Continue"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $py -m ruff check airsender tools tests; if ($LASTEXITCODE -ne 0) { exit 1 }
& $py -m ruff format --check airsender tools tests; if ($LASTEXITCODE -ne 0) { Write-Host "執行 .venv\Scripts\python.exe -m ruff format airsender tools tests 修正格式"; exit 1 }
& $py -m unittest discover -s tests -q; exit $LASTEXITCODE
