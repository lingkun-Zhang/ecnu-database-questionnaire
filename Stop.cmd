@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1" %*
set "stopExitCode=%errorlevel%"
if not "%stopExitCode%"=="0" pause
exit /b %stopExitCode%
