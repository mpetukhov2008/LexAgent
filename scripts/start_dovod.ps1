param(
    [ValidateRange(0, 65525)][int]$Port = 8010,
    [switch]$NoBrowser,
    [switch]$SkipOcr,
    [switch]$RequireOcr,
    [switch]$SmokeTest,
    [switch]$PrivatePython
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$env:PYTHONUTF8 = '1'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Test-TaskPython([string]$Executable) {
    if (-not $Executable) { return $false }
    try {
        & $Executable -c "import sys,struct; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,15) and struct.calcsize('P') == 8 else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Get-TaskPython {
    $taskManifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'windows-python.json') -Raw | ConvertFrom-Json
    $taskRuntime = Join-Path $taskRoot ('data\python-' + $taskManifest.version)
    $taskRuntimeExe = Join-Path $taskRuntime 'python.exe'
    if (Test-TaskPython $taskRuntimeExe) { return $taskRuntimeExe }
    if (-not $PrivatePython) {
        $taskLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($taskLauncher) {
            foreach ($taskVersion in @('-3.14','-3.13','-3.12','-3.11','-3.10')) {
                try {
                    $taskCandidate = & $taskLauncher.Source $taskVersion -c 'import sys; print(sys.executable)' 2>$null
                    if ($LASTEXITCODE -eq 0 -and (Test-TaskPython $taskCandidate)) { return $taskCandidate }
                } catch { continue }
            }
        }
        $taskCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($taskCommand -and $taskCommand.Source -notlike '*\WindowsApps\*' -and (Test-TaskPython $taskCommand.Source)) {
            return $taskCommand.Source
        }
    }
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or -not [Environment]::Is64BitOperatingSystem) {
        throw 'Автоматическая установка рассчитана на Windows x64. Для ARM/macOS/Linux смотрите README.'
    }
    Write-Host ('Распаковываем официальный Python ' + $taskManifest.version + ' в папку проекта...')
    New-Item -ItemType Directory -Path (Join-Path $taskRoot 'data') -Force | Out-Null
    $taskArchive = Join-Path $taskRoot ('data\python-' + $taskManifest.version + '-runtime.zip')
    $taskExpectedHash = $taskManifest.sha256
    if (-not (Test-Path -LiteralPath $taskArchive) -or (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLower() -ne $taskExpectedHash) {
        Invoke-WebRequest -UseBasicParsing -Uri $taskManifest.url -OutFile $taskArchive -TimeoutSec 120
    }
    if ((Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLower() -ne $taskExpectedHash) {
        throw 'Контрольная сумма Python не совпала. Подготовка остановлена.'
    }
    if (Test-Path -LiteralPath $taskRuntime) {
        throw 'Локальная папка Python повреждена. Удалите только data/python-3.14.10 и повторите запуск.'
    }
    $taskExtract = Join-Path $taskRoot ('data\python-extract-' + [Guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskExtract
    if (-not (Test-TaskPython (Join-Path $taskExtract 'python.exe'))) {
        throw 'Распакованный Python не запускается. Попробуйте короткую папку, например C:\Dovod.'
    }
    # Both resolved paths are inside this project's data directory before moving.
    $taskDataPrefix = [IO.Path]::GetFullPath((Join-Path $taskRoot 'data')) + [IO.Path]::DirectorySeparatorChar
    if (-not [IO.Path]::GetFullPath($taskExtract).StartsWith($taskDataPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFullPath($taskRuntime).StartsWith($taskDataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Недопустимый путь локального Python.'
    }
    Move-Item -LiteralPath $taskExtract -Destination $taskRuntime
    return $taskRuntimeExe
}

$taskSetupLock = $null
try {
    if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'requirements.lock.txt'))) {
        throw 'Распакуйте весь ZIP в отдельную папку. Не запускайте файл внутри архива.'
    }
    New-Item -ItemType Directory -Path (Join-Path $taskRoot 'data') -Force | Out-Null
    try {
        $taskSetupLock = [IO.File]::Open((Join-Path $taskRoot 'data\startup.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] { throw 'Подготовка уже идёт в другом окне. Дождитесь её окончания.' }
    $taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        $taskBasePython = Get-TaskPython
        Write-Host 'Готовим окружение проекта...'
        & $taskBasePython -m venv (Join-Path $taskRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python. Проверьте доступ к папке.' }
    }
    if (-not (Test-TaskPython $taskPython)) {
        throw 'Окружение .venv повреждено или перенесено с другого компьютера. Удалите только папку .venv и повторите запуск.'
    }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    $taskLockHash = (Get-FileHash -LiteralPath 'requirements.lock.txt' -Algorithm SHA256).Hash
    $taskMarker = '.venv\lexagent-requirements.sha256'
    $taskInstalledHash = if (Test-Path -LiteralPath $taskMarker) { (Get-Content -LiteralPath $taskMarker -Raw).Trim() } else { '' }
    if ($taskInstalledHash -ne $taskLockHash) {
        Write-Host 'Устанавливаем зависимости. При первом запуске нужен интернет, это может занять несколько минут...'
        & $taskPython -m pip install --disable-pip-version-check -r requirements.lock.txt
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости. Проверьте интернет и сообщения выше; затем повторите запуск.' }
        Set-Content -LiteralPath $taskMarker -Value $taskLockHash -NoNewline -Encoding ascii
    }
    if (-not $SkipOcr) {
        & $taskPython -c 'from app.pdf_worker import ocr_ready; import sys; sys.exit(0 if ocr_ready() else 1)'
        if ($LASTEXITCODE -ne 0) {
            try {
                Write-Host 'Подготавливаем распознавание сканов. Windows может запросить разрешение...'
                & (Join-Path $PSScriptRoot 'setup_ocr.ps1')
            } catch {
                if ($RequireOcr) { throw }
                Write-Warning 'OCR пока не установлен. Демо и текстовые PDF работают; сканы потребуют настройки в разделе «Подключения».'
            }
        }
    }
    if (Test-Path -LiteralPath 'data\pg\PG_VERSION') {
        $taskPgBin = if ($env:PG_BIN) { $env:PG_BIN } else { 'C:\Program Files\PostgreSQL\17\bin' }
        $taskPgCtl = Join-Path $taskPgBin 'pg_ctl.exe'
        if (-not (Test-Path -LiteralPath $taskPgCtl)) { throw 'Для прежней локальной базы укажите PG_BIN. Новому ZIP PostgreSQL не нужен.' }
        & $taskPgCtl -D data/pg status | Out-Null
        if ($LASTEXITCODE -ne 0) {
            & $taskPgCtl -D data/pg -l data/pg-server.log -o '-p 55432 -h 127.0.0.1' start
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить настроенный PostgreSQL.' }
        }
    }
    $taskServerArguments = @('-m','app.desktop','--port',[string]$Port)
    if ($NoBrowser -or $SmokeTest) { $taskServerArguments += '--no-browser' }
    if ($SmokeTest) { $taskServerArguments += '--smoke-test' }
    if ($RequireOcr) { $taskServerArguments += '--require-ocr' }
    $taskSetupLock.Dispose()
    $taskSetupLock = $null
    & $taskPython @taskServerArguments
    exit $LASTEXITCODE
} catch {
    Write-Host ''
    Write-Host ('Запуск остановлен: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Краткая инструкция: START_HERE.txt. Полное описание: README.md.'
    exit 1
} finally {
    if ($taskSetupLock) { $taskSetupLock.Dispose() }
}
