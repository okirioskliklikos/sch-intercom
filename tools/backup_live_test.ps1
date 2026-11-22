# Αυτόματη δοκιμή backup (μόνο προσωρινοί φάκελοι).
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
$env:PYTHONIOENCODING = "utf-8"
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
