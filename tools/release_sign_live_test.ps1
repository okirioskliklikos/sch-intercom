# Φάση 5B1: αυτόματη πραγματική δοκιμή υπογραφής εκδόσεων (10 βήματα, PASS/FAIL).
# Πρώτα τα unit tests, μετά: πραγματικό δέντρο έκδοσης, κλειδί δοκιμής, υπογραφή .sicu,
# επαλήθευση, απόρριψη λάθος κλειδιού/κωδικού, αλλοιωμένου αρχείου/manifest, downgrade,
# αποσυμπίεση. Μόνο προσωρινοί φάκελοι· το πραγματικό κλειδί δεν χρησιμοποιείται.
# Χρειάζεται internet. Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\release_sign_live_test.ps1
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try {
    if (Test-Path (Join-Path $root ".venv\Scripts\python.exe")) {
        $python = Join-Path $root ".venv\Scripts\python.exe"
    } else {
        $python = "python"
    }
    & $python -m unittest tests.test_release_package -v
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: unit tests υπογραφής"; exit 1 }
    & $python tools\release_sign_live_test.py @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
