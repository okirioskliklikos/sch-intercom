# Αυτόματη πραγματική δοκιμή 4.2C2 (κλειδί TLS του Manager με DPAPI), Manager σε Windows.
# Πρώτα τα unit tests, μετά 8 βήματα με πραγματικό Manager και Client.
# Μόνο προσωρινοί φάκελοι, loopback, θύρες 51555/51556/51559.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\manager_key_live_test.ps1
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
    & $python -m unittest discover -s tests -p test_manager_key.py -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAIL: unit tests 4.2C2"
        exit 1
    }
    & $python tools\manager_key_live_test.py
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
