@echo off
REM Double-click to run HVACTrend in this window (Ctrl+C or close the window to stop).
REM To run it in the background and after reboots instead: docs\install.md, "Run as a service".
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo HVACTrend is not installed yet: double-click install.bat first.
  pause
  exit /b 1
)
if not exist "config\config.yaml" (
  echo There is no config\config.yaml yet: run  .venv\Scripts\python scripts\configure.py
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\serve.py --open
pause
