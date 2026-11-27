# Αυτόματη πραγματική δοκιμή 5B3: Ρυθμίσεις → «Ενημερώσεις» (εισαγωγή πακέτου, αυτόματες,
# «Έναρξη τώρα», κατάσταση ανά υπολογιστή, backup/επαναφορά με πακέτα, Εφεδρικός, αποτυχία).
# Πρώτα τα unit tests, μετά 15 βήματα: το πραγματικό παράθυρο Director και τρεις Clients.
# Μόνο προσωρινοί φάκελοι, loopback, θύρες 51855-51867· πακέτα με κλειδί δοκιμής.
# Θα ανοίξει για λίγο το παράθυρο. Στιγμιότυπα: %USERPROFILE%\sch-intercom-test-screens
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\updates_gui_live_test.ps1
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
    & $python -m unittest tests.test_updates_page tests.test_backup_updates tests.test_updates -v
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: unit tests Ενημερώσεων"; exit 1 }
    & $python tools\updates_gui_live_test.py
    $status = $LASTEXITCODE
    if ($status -ne 0) { exit $status }
    exit 0
} finally {
    Pop-Location
}
