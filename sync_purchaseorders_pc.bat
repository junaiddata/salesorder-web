@echo off
REM Windows batch script to run Purchase Order sync every 30 minutes
REM Double-click in the morning to start; it runs until you close the window or press Ctrl+C
REM Logs are also saved to logs\sync_purchaseorders.log

cd /d %~dp0

if not exist "logs" mkdir logs

set LOG_FILE=logs\sync_purchaseorders.log
set PYTHONUNBUFFERED=1

echo [%date% %time%] Starting Purchase Order sync (sync_purchaseorders_pc.py)... >> "%LOG_FILE%"
echo [%date% %time%] Working directory: %CD% >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

if not exist "sync_purchaseorders_pc.py" (
    echo ERROR: sync_purchaseorders_pc.py not found in %CD%
    echo [%date% %time%] ERROR: sync_purchaseorders_pc.py not found >> "%LOG_FILE%"
    pause
    exit /b 1
)

REM Run with pythonw (background, no console window)
REM Do NOT redirect here - the Python script writes to logs\sync_purchaseorders.log itself.
REM Redirecting causes "Permission denied" because both batch and Python would open the same file.
pythonw.exe -u sync_purchaseorders_pc.py
