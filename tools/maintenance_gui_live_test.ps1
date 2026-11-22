# Αυτόματη πραγματική δοκιμή: Ασφάλεια → «Πιστοποιητικά» και Ρυθμίσεις → «Συντήρηση».
# Πρώτα τα unit tests (backup + συντήρηση), μετά 14 βήματα με το πραγματικό παράθυρο
# Director, Manager, Client και κλείδωμα. Μόνο προσωρινοί φάκελοι, loopback,
# θύρες 51555/51556/51559. Δεν χρειάζεται μικρόφωνο. Θα ανοίξει για λίγο το παράθυρο.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\maintenance_gui_live_test.ps1
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
    & $python -m unittest discover -s tests -p "test_backup.py" -v
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: unit tests backup"; exit 1 }
    & $python -m unittest discover -s tests -p "test_maintenance.py" -v
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: unit tests συντήρησης"; exit 1 }
    & $python tools\maintenance_gui_live_test.py
    $status = $LASTEXITCODE
    if ($status -ne 0) { exit $status }
    exit 0
} finally {
    Pop-Location
}
