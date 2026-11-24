# Αυτόματη πραγματική δοκιμή: Ρυθμίσεις → «Εμφάνιση» (κλίμακα περιβάλλοντος).
# Πρώτα τα unit tests (κλίμακα + συντήρηση), μετά 12 βήματα με το πραγματικό παράθυρο
# Director, Manager και Client: προεπιλογή 90% στα Windows, επιλογή 80%, επανεκκίνηση,
# μέτρηση μεγεθών, 125%, επιστροφή. Μόνο προσωρινοί φάκελοι, loopback, θύρες
# 51555/51556/51559. Δεν χρειάζεται μικρόφωνο. Θα ανοίξει για λίγο το παράθυρο.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\ui_scale_live_test.ps1
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
    & $python -m unittest tests.test_ui_scale tests.test_ui_theme tests.test_maintenance -v
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: unit tests κλίμακας/συντήρησης"; exit 1 }
    & $python tools\ui_scale_live_test.py
    $status = $LASTEXITCODE
    if ($status -ne 0) { exit $status }
    exit 0
} finally {
    Pop-Location
}
