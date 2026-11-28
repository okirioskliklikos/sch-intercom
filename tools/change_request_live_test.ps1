# Φάση 5C2: «Αλλαγή…» αίθουσας/ονόματος· αυτόματη πραγματική δοκιμή Windows, 16 βήματα PASS/FAIL.
# Χρησιμοποιεί αποκλειστικά προσωρινούς φακέλους και loopback 127.0.0.1.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\change_request_live_test.ps1 [-Visible]
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
    & $python -m unittest tests.test_change_request tests.test_change_request_director tests.test_client_tray tests.test_enrollment tests.test_security_page tests.test_keepalive tests.test_ui_scale -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL: unit tests 5C2'
        exit 1
    }
    $extra = @()
    if ($Visible) { $extra += '--visible' }
    & $python tools\change_request_live_test.py @extra
    $status = $LASTEXITCODE
    exit $status
} finally {
    Pop-Location
}
