# Αυτόματη πραγματική δοκιμή 4.2D2: GUI Κύριου/Εφεδρικού (μπάρα, ειδοποιήσεις, κάρτα
# «Ρόλος Manager», διάλογοι, «Μητρώο υπολογιστών»). Πρώτα τα unit tests, μετά 16 βήματα:
# Manager A (CLI) και Manager B με το πραγματικό παράθυρο Director, δύο Clients.
# Μόνο προσωρινοί φάκελοι, loopback, θύρες 51555-51569. Δεν χρειάζεται μικρόφωνο.
# Θα ανοίξει για λίγο το παράθυρο. Στιγμιότυπα: %USERPROFILE%\sch-intercom-test-screens
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\manager_role_gui_live_test.ps1
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
    & $python -m unittest discover -s tests -p "test_manager_role_gui.py" -v
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: unit tests GUI Κύριου/Εφεδρικού"; exit 1 }
    & $python tools\manager_role_gui_live_test.py
    $status = $LASTEXITCODE
    if ($status -ne 0) { exit $status }
    exit 0
} finally {
    Pop-Location
}
