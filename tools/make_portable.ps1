# 4.4 βήμα 2: φτιάχνει το dist\sch-intercom-client.zip (φορητός βοηθός δοκιμής για Clients).
# Τρέχει στον υπολογιστή με τον κώδικα και internet:
#   powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1
# Βλ. tools/make_portable.py και docs/phase4-4.md §6.
param(
    [string]$PythonVersion = '',
    [string]$EmbedZip = ''
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
    $arguments = @('tools\make_portable.py')
    if ($PythonVersion) { $arguments += @('--python-version', $PythonVersion) }
    if ($EmbedZip) { $arguments += @('--embed-zip', $EmbedZip) }
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
