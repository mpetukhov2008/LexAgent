$ErrorActionPreference = 'Stop'
$taskSource = Split-Path -Parent $PSScriptRoot
$taskTemp = Join-Path ([IO.Path]::GetTempPath()) ('dovod-zip-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskTemp | Out-Null
$taskArchive = Join-Path $taskTemp 'Dovod.zip'
$taskExtracted = Join-Path $taskTemp 'Довод ZIP проверка'
& git -C $taskSource archive --format=zip --output $taskArchive HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not create source ZIP.' }
Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskExtracted
foreach ($taskName in @('START_DOVOD.cmd','START_HERE.txt','README.md','README.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $taskExtracted $taskName))) { throw ('ZIP missing ' + $taskName) }
}
$taskEntry = Join-Path $taskExtracted 'START_DOVOD.cmd'
$env:DOVOD_NO_PAUSE = '1'
& $taskEntry -PrivatePython -SmokeTest -NoBrowser -RequireOcr -Port 0
if ($LASTEXITCODE -ne 0) { throw 'First ZIP startup failed.' }
$taskEnv = Join-Path $taskExtracted '.env'
Add-Content -LiteralPath $taskEnv -Value '# User settings must survive repeated startup.' -Encoding utf8
$taskBefore = (Get-FileHash -LiteralPath $taskEnv -Algorithm SHA256).Hash
& $taskEntry -SmokeTest -NoBrowser -RequireOcr -Port 0
if ($LASTEXITCODE -ne 0) { throw 'Repeated ZIP startup failed.' }
if ((Get-FileHash -LiteralPath $taskEnv -Algorithm SHA256).Hash -ne $taskBefore) {
    throw 'Repeated startup changed user settings.'
}
Write-Host 'ZIP startup, private Python, dependencies, real OCR, Cyrillic path and preserved .env: passed.'
