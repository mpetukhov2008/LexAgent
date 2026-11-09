param([int]$Port = 8010)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python' }
}
$taskLockHash = (Get-FileHash -LiteralPath 'requirements.lock.txt' -Algorithm SHA256).Hash
$taskLockMarker = '.venv/lexagent-requirements.sha256'
$taskInstalledHash = if (Test-Path -LiteralPath $taskLockMarker) {
    (Get-Content -LiteralPath $taskLockMarker -Raw).Trim()
} else { '' }
if ($taskInstalledHash -ne $taskLockHash) {
    & .venv/Scripts/python.exe -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось обновить зависимости проекта' }
    Set-Content -LiteralPath $taskLockMarker -Value $taskLockHash -NoNewline -Encoding ascii
}
if (Test-Path -LiteralPath 'data/pg/PG_VERSION') {
    $pgBinPath = if ($env:PG_BIN) { $env:PG_BIN } else { 'C:\Program Files\PostgreSQL\17\bin' }
    & "$pgBinPath/pg_ctl.exe" -D data/pg status | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & "$pgBinPath/pg_ctl.exe" -D data/pg -l data/pg-server.log -o '-p 55432 -h 127.0.0.1' start
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL не запущен' }
    }
}
& .venv/Scripts/python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port $Port
