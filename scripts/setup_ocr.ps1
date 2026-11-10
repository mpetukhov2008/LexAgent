$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskData = Join-Path $taskRoot 'data'
$taskEngine = Join-Path $taskData 'ocr-engine-5.5.3'
$taskExecutable = Join-Path $taskEngine 'tesseract.exe'
New-Item -ItemType Directory -Path $taskData -Force | Out-Null
if (-not (Test-Path -LiteralPath $taskExecutable)) {
    if (Test-Path -LiteralPath $taskEngine) {
        throw 'OCR target directory already exists without a valid executable; inspect it first.'
    }
    $taskInstaller = Join-Path $taskData 'tesseract-5.5.3-installer.exe'
    Invoke-WebRequest -Uri 'https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe' -OutFile $taskInstaller
    $taskExpectedHash = 'bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4'
    if ((Get-FileHash -LiteralPath $taskInstaller -Algorithm SHA256).Hash.ToLower() -ne $taskExpectedHash) {
        throw 'Tesseract installer checksum mismatch.'
    }
    $taskProcess = Start-Process -FilePath $taskInstaller -ArgumentList @('/S', "/D=$taskEngine") -WindowStyle Hidden -PassThru -Wait
    if ($taskProcess.ExitCode -ne 0) { throw 'Tesseract installation failed.' }
}
$taskLanguage = Join-Path $taskEngine 'tessdata\rus.traineddata'
if (-not (Test-Path -LiteralPath $taskLanguage)) {
    Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/rus.traineddata' -OutFile $taskLanguage
}
if ((Get-FileHash -LiteralPath $taskLanguage -Algorithm SHA256).Hash.ToLower() -ne 'e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225') {
    throw 'Russian OCR model checksum mismatch.'
}
Push-Location -LiteralPath $taskEngine
try {
    & $taskExecutable --tessdata-dir tessdata --list-langs
    if ($LASTEXITCODE -ne 0) { throw 'Tesseract language check failed.' }
} finally { Pop-Location }
