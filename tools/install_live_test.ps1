# Φάση 5A: αυτόματη πραγματική δοκιμή του εγκαταστάτη Client (15 βήματα, PASS/FAIL).
# Πρώτα τα unit tests, μετά: build δύο εκδόσεων + setup.exe (Inno Setup 6), αθόρυβη
# εγκατάσταση σε προσωρινό φάκελο, αυτόματη εκκίνηση (HKCU\Run), ένα αντίγραφο,
# --restart/--stop/--start, επανεκκίνηση μετά από τερματισμό, επανεγκατάσταση,
# αναβάθμιση, απεγκατάσταση και «Πλήρης αφαίρεση». Η πραγματική ταυτότητα δεν αγγίζεται.
# Χρειάζεται internet (embeddable Python + wheels). Δεν χρειάζεται admin ή ηχεία.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\install_live_test.ps1
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
    & $python -m unittest tests.test_client_install -v
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: unit tests εγκατάστασης"; exit 1 }
    & $python tools\install_live_test.py @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
