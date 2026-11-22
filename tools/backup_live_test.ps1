# Αυτόματη πραγματική δοκιμή 4.2C (docs/phase4-2c-backup.md §11), Manager σε Windows.
# Πρώτα τα unit tests του backup, μετά 18 βήματα με πραγματικό Manager, Client και ήχο
# (χρειάζεται μικρόφωνο και ηχεία). Μόνο προσωρινοί φάκελοι, loopback, θύρες 51555/51556/51559.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\backup_live_test.ps1
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
    & $python -m unittest discover -s tests -p test_backup.py -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAIL: unit tests 4.2C"
        exit 1
    }
    & $python tools\backup_live_test.py
    $status = $LASTEXITCODE
    if ($status -ne 0) { exit $status }
    exit 0
} finally {
    Pop-Location
}
