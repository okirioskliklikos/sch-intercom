# Αυτόματη δοκιμή 4.2A1/4.2A2 — πλευρά Client (Windows). Βλ. tools/mtls_live_test.py.
# Εκτέλεση: powershell -ExecutionPolicy Bypass -File tools\mtls_live_test.ps1
param([string]$DirectorHost = "")
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Join-Path $PSScriptRoot "..")
$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "py" }
$arguments = @("tools\mtls_live_test.py", "client")
if ($DirectorHost) { $arguments += @("--director-host", $DirectorHost) }
& $py @arguments
exit $LASTEXITCODE
