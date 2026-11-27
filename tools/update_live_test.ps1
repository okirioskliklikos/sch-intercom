# Φάση 5B2a: διανομή ενημερώσεων Client· αυτόματη πραγματική δοκιμή Windows, 15 βήματα PASS/FAIL.
# Μόνο loopback 127.0.0.1 και προσωρινοί φάκελοι· πακέτα με κλειδί δοκιμής (όχι το πραγματικό).
# Η κανονική εγκατάσταση του Client δεν αγγίζεται (η εναλλαγή έκδοσης είναι το 5B2b).
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\update_live_test.ps1
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
    & $python -m unittest tests.test_updates tests.test_release_package tests.test_keepalive -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL: unit tests ενημερώσεων'
        exit 1
    }
    & $python tools\update_live_test.py
    $status = $LASTEXITCODE
    exit $status
} finally {
    Pop-Location
}
