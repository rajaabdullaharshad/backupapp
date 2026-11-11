$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (Get-Command python -ErrorAction SilentlyContinue) {
    python "src\frappe_auto_deploy\main.py"
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 "src\frappe_auto_deploy\main.py"
    exit $LASTEXITCODE
}

Write-Host "Python not found. Install Python 3 and try again."
exit 1
