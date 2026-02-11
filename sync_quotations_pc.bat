@echo off
REM Windows batch script to run quotation sync continuously every 10 minutes
REM This script runs the Python scheduler script (sync_quotations_pc.py) which handles the loop
REM Uses pythonw to run without showing console window
REM Logs are saved to logs/sync_quotations.log

REM Change to the directory where this batch file is located (should be salesorder/)
cd /d %~dp0

REM Set log file path (in logs directory)
set LOG_FILE=logs\sync_quotations.log

REM Ensure logs directory exists
if not exist "logs" mkdir logs

echo [%date% %time%] Starting quotation sync scheduler (sync_quotations_pc.py)... >> "%LOG_FILE%"
echo [%date% %time%] Working directory: %CD% >> "%LOG_FILE%"
echo [%date% %time%] Python executable: pythonw >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

REM Check if Python script exists
if not exist "sync_quotations_pc.py" (
    echo [%date% %time%] ERROR: sync_quotations_pc.py not found in %CD% >> "%LOG_FILE%"
    exit /b 1
)

REM Activate virtual environment if you have one
REM call venv\Scripts\activate

REM Set environment variable for unbuffered output
set PYTHONUNBUFFERED=1

REM Run the Python scheduler script using pythonw (runs in background, no console window)
REM The Python script has its own loop, so this will run continuously
REM Use -u flag for unbuffered output to ensure logs are written immediately
REM Note: pythonw doesn't show console and returns immediately, so errors go to log file
REM Run pythonw directly - it will detach and run in background
pythonw.exe -u sync_quotations_pc.py >> "%LOG_FILE%" 2>&1

REM Log that we started the process
echo [%date% %time%] Pythonw command executed. Process should be running in background. >> "%LOG_FILE%"
echo [%date% %time%] Check the log file for sync activity. >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

REM Exit batch file - pythonw process continues independently
exit /b 0
