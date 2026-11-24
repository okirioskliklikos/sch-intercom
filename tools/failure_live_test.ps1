# Φάση 4.3B: σενάρια σφαλμάτων· αυτόματη πραγματική δοκιμή Windows, 16 βήματα PASS/FAIL.
# Ο Board1 αλλάζει προσωρινά την κύρια ένταση (25% -> 60%)· στο τέλος επανέρχεται η αρχική.
# Χρησιμοποιεί αποκλειστικά προσωρινούς φακέλους και loopback 127.0.0.1.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\failure_live_test.ps1
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
    & $python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL: πλήρης σουίτα αυτοματοποιημένων ελέγχων'
        exit 1
    }
    & $python tools\failure_live_test.py
    $status = $LASTEXITCODE
    exit $status
} finally {
    Pop-Location
}
