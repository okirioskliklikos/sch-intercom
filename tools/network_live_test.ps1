# 4.4: δοκιμή σε πραγματικό δίκτυο (Windows). Βλ. tools/network_live_test.py.
# Client:    powershell -ExecutionPolicy Bypass -File tools\network_live_test.ps1 [-DirectorHost IP]
# Director:  powershell -ExecutionPolicy Bypass -File tools\network_live_test.ps1 -Role director -Clients 2
# Προσωρινά κλειδιά/μητρώο σε φάκελο δοκιμής που διαγράφεται στο τέλος.
param(
    [ValidateSet('client', 'director')][string]$Role = 'client',
    [string]$DirectorHost = '',
    [int]$Clients = 1,
    [int]$Virtual = 20,
    [ValidateSet('', 'lan', 'wifi')][string]$Link = '',
    [switch]$ShowProcesses
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
    $arguments = @('tools\network_live_test.py', $Role)
    if ($Role -eq 'director') {
        $arguments += @('--clients', "$Clients", '--virtual', "$Virtual")
    } elseif ($DirectorHost) {
        $arguments += @('--director-host', $DirectorHost)
    }
    if ($Link) { $arguments += @('--link', $Link) }
    if ($ShowProcesses) { $arguments += '--verbose' }
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
