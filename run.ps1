param([int]$Port = 8010)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python' }
    & .venv/Scripts/python.exe -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }
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
