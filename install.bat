@echo off
REM Double-click to install HVACTrend (runs install.ps1; no administrator rights needed).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
