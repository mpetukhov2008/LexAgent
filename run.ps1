param([int]$Port = 8010)
& (Join-Path $PSScriptRoot "scripts/start_dovod.ps1") -Port $Port
exit $LASTEXITCODE
