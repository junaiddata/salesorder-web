@echo off
REM Windows batch script to run sync continuously every 5 minutes
REM This script runs the Python scheduler script (sync_sales_data_cron.py) which handles the loop
REM Uses pythonw to run without showing console window
REM Logs are saved to logs/sync_sales_data.log (same location as other sync logs)

REM Change to the directory where this batch file is located (should be salesorder/)
cd /d %~dp0

REM Set log file path (in logs directory, same as other sync logs)
set LOG_FILE=logs\sync_sales_data.log

REM Ensure logs directory exists
if not exist "logs" mkdir logs

echo [%date% %time%] Starting sync scheduler (sync_sales_data_cron.py)... >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

REM Activate virtual environment if you have one
REM call venv\Scripts\activate

REM Run the Python scheduler script using pythonw (runs in background, no console window)
REM The Python script has its own loop, so this will run continuously
pythonw sync_sales_data_cron.py >> "%LOG_FILE%" 2>&1

REM This line should never be reached unless the Python script exits
echo [%date% %time%] Sync scheduler stopped unexpectedly. >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"
