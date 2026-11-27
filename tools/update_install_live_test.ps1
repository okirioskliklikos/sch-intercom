# Φάσεις 5B2b/5B2c: εναλλαγή έκδοσης Client, rollback, λήψη μόνο των αρχείων που άλλαξαν και
# απεγκατάσταση μετά από ενημερώσεις· αυτόματη πραγματική δοκιμή Windows, 17 βήματα.
# Πραγματικός εγκαταστάτης (Inno Setup 7 ή 6) σε προσωρινό φάκελο, δικό του όνομα στο HKCU\Run,
# μόνο loopback 127.0.0.1, πακέτα με κλειδί δοκιμής. Θέλει internet (embeddable Python).
# Αν υπάρχει πραγματική εγκατάσταση Client στον χρήστη, η δοκιμή δεν τρέχει.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\update_install_live_test.ps1
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
    & $python -m unittest tests.test_update_switch tests.test_updates tests.test_update_delta tests.test_client_install tests.test_release_package -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL: unit tests εναλλαγής έκδοσης'
        exit 1
    }
    & $python tools\update_install_live_test.py --installer
    $status = $LASTEXITCODE
    exit $status
} finally {
    Pop-Location
}
