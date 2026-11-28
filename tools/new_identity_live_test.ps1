# Φάση 5C3: νέα ταυτότητα, κλώνοι με σύσταση, ανάκληση κλώνων· αυτόματη πραγματική δοκιμή Windows, 16 βήματα PASS/FAIL.
# Χρησιμοποιεί αποκλειστικά προσωρινούς φακέλους και loopback 127.0.0.1.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\new_identity_live_test.ps1 [-Keep]
param([switch]$Keep)
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
    & $python -m unittest tests.test_new_identity tests.test_enrollment tests.test_security_page tests.test_keepalive tests.test_change_request tests.test_registry_seen tests.test_clone_and_full -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL: unit tests 5C3'
        exit 1
    }
    $extra = @()
    if ($Keep) { $extra += '--keep' }
    & $python tools\new_identity_live_test.py @extra
    $status = $LASTEXITCODE
    exit $status
} finally {
    Pop-Location
}
