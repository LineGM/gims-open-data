param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SyncArguments
)
$ErrorActionPreference = 'Stop'
$syncRepo = Split-Path -Parent $PSScriptRoot
$syncPython = Join-Path $syncRepo '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $syncPython)) {
    throw 'Missing .venv. Run the Python 3.12 setup in README.md first.'
}
Push-Location -LiteralPath $syncRepo
try {
    & $syncPython -m gims_open_data sync @SyncArguments
    $syncExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $syncExitCode
