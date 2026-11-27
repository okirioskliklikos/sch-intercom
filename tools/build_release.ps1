# Φάση 5A: φτιάχνει το δέντρο έκδοσης του Client και το dist\sch-intercom-client-<έκδοση>-setup.exe.
#   powershell -ExecutionPolicy Bypass -File tools\build_release.ps1 [-Version 1.0.0] [-NoSetup] [-Sign <κλειδί.pem>]
# Χρειάζεται internet και Inno Setup 7 ή 6. Βλ. tools/build_release.py και docs/phase5.md §4.1.
param(
    [string]$Version = '',
    [string]$PythonVersion = '',
    [string]$EmbedZip = '',
    [switch]$NoSetup,
    [string]$Sign = ''
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
    $arguments = @('tools\build_release.py')
    if ($Version) { $arguments += @('--version', $Version) }
    if ($PythonVersion) { $arguments += @('--python-version', $PythonVersion) }
    if ($EmbedZip) { $arguments += @('--embed-zip', $EmbedZip) }
    if ($NoSetup) { $arguments += '--no-setup' }
    if ($Sign) { $arguments += @('--sign', $Sign) }
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
