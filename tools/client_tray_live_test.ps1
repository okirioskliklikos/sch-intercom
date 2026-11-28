# Φάση 5C1: tray και παράθυρο Client· αυτόματη πραγματική δοκιμή Windows, 15 βήματα PASS/FAIL.
# Χρησιμοποιεί αποκλειστικά προσωρινούς φακέλους και loopback 127.0.0.1.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\client_tray_live_test.ps1 [-Visible]
param([switch]$Visible)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try {
    if (Test-Path (Join-Path $root '.venv\Scripts\python.exe')) {
        $python = Join-Path $root '.venv\Scripts\python.exe'
    } else {
        $python = 'python'
    }
    & $python -m unittest tests.test_client_tray tests.test_ui_theme tests.test_reconnect tests.test_failure_scenarios -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL: unit tests 5C1'
        exit 1
    }
    $extra = @()
    if ($Visible) { $extra += '--visible' }
    & $python tools\client_tray_live_test.py @extra
    $status = $LASTEXITCODE
    exit $status
} finally {
    Pop-Location
}
