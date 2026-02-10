@echo off
REM Windows batch script to run customer finance summary sync continuously every 1 hour
REM This script runs the Python scheduler script (sync_customer_finance_cron.py) which handles the loop
REM Uses pythonw to run without showing console window
REM Logs are saved to logs/sync_customer_finance.log

REM Change to the directory where this batch file is located (should be salesorder/)
cd /d %~dp0

REM Set log file path (in logs directory)
set LOG_FILE=logs\sync_customer_finance.log

REM Ensure logs directory exists
if not exist "logs" mkdir logs

echo [%date% %time%] Starting customer finance sync scheduler (sync_customer_finance_cron.py)... >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

REM Activate virtual environment if you have one
REM call venv\Scripts\activate

REM Set environment variable for unbuffered output
set PYTHONUNBUFFERED=1

REM Run the Python scheduler script using pythonw (runs in background, no console window)
REM The Python script has its own loop, so this will run continuously
REM Use -u flag for unbuffered output to ensure logs are written immediately
pythonw -u sync_customer_finance_cron.py >> "%LOG_FILE%" 2>&1

REM This line should never be reached unless the Python script exits
echo [%date% %time%] Customer finance sync scheduler stopped unexpectedly. >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"
