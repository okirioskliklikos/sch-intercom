# Φάση 5: φτιάχνει τον φάκελο dist\field-test (και το zip) για τη δοκιμή σε καθαρά Windows:
# setup.exe 0.6.0 και πακέτα 0.6.1-0.6.4 υπογεγραμμένα με το πραγματικό κλειδί (ζητά τον κωδικό του).
#   powershell -ExecutionPolicy Bypass -File tools\field_test_build.ps1 -Key <κλειδί.pem>
# Χρειάζεται internet και Inno Setup 7 ή 6. Βλ. tools/field_test_build.py.
param(
    [Parameter(Mandatory = $true)][string]$Key,
    [string]$PythonVersion = '',
    [string]$EmbedZip = '',
    [switch]$NoZip
)
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
    & $python -m unittest tests.test_field_test -v
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL: unit tests της δοκιμής πεδίου'
        exit 1
    }
    $arguments = @('tools\field_test_build.py', '--key', $Key)
    if ($PythonVersion) { $arguments += @('--python-version', $PythonVersion) }
    if ($EmbedZip) { $arguments += @('--embed-zip', $EmbedZip) }
    if ($NoZip) { $arguments += '--no-zip' }
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
