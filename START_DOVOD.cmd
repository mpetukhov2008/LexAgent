@echo off
setlocal
rem Keep Windows PowerShell modules when launched from PowerShell 7.
set "PSModulePath=%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules;%ProgramFiles%\WindowsPowerShell\Modules"
cd /d "%~dp0"
if not exist "%~dp0scripts\start_dovod.ps1" (
  echo Extract the entire ZIP before starting Dovod.
  if not "%DOVOD_NO_PAUSE%"=="1" pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_dovod.ps1" %*
if errorlevel 1 (
  echo Start failed. Read the message above or START_HERE.txt.
  if not "%DOVOD_NO_PAUSE%"=="1" pause
  exit /b 1
)
endlocal
