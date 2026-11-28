# Φάση 5: δοκιμή του Client σε καθαρά Windows (tools/field_test.py, docs/phase5.md §7.1).
# Τρέχει από τον φάκελο field-test (tools\field_test_build.ps1) ή από το repository αν υπάρχει
# το dist\field-test. Συνήθως μέσω του field-test.cmd:
#   field-test.cmd <IP του Director>   |   field-test.cmd -Resume   |   field-test.cmd -Cleanup
param(
    [Parameter(Position = 0)][string]$DirectorHost = '',
    [switch]$Resume,
    [switch]$Cleanup,
    [switch]$NoReboot
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$bundle = $PSScriptRoot
if (-not (Test-Path (Join-Path $bundle 'python\python.exe'))) {
    $bundle = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\field-test'
}
$python = Join-Path $bundle 'python\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'Δεν βρέθηκε ο φάκελος field-test. Φτιάξτε τον πρώτα: tools\field_test_build.ps1 -Key <κλειδί.pem>'
    exit 2
}
$arguments = @((Join-Path $bundle 'app\tools\field_test.py'), 'client', '--bundle', $bundle)
if ($DirectorHost) { $arguments += @('--director-host', $DirectorHost) }
if ($Resume) { $arguments += '--resume' }
if ($Cleanup) { $arguments += '--cleanup' }
if ($NoReboot) { $arguments += '--no-reboot' }
& $python @arguments
exit $LASTEXITCODE
