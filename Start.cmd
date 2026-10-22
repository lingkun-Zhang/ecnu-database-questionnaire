@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
set "startExitCode=%errorlevel%"
if not "%startExitCode%"=="0" pause
exit /b %startExitCode%
