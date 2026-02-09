@echo off
REM Windows batch script to run sync ONCE
REM This script runs the Django management command to sync AR Invoices and Credit Memos
REM Uses pythonw to run without showing console window
REM Logs are saved to logs/sync_sales_data.log (same location as other sync logs)
REM 
REM NOTE: This script runs sync ONCE. For continuous syncing every 5 minutes,
REM use sync_sales_data_cron.py instead, or set up Windows Task Scheduler to run this .bat every 5 minutes.

REM Change to the directory where this batch file is located (should be salesorder/)
cd /d %~dp0

REM Set log file path (in logs directory, same as other sync logs)
set LOG_FILE=logs\sync_sales_data.log

REM Ensure logs directory exists
if not exist "logs" mkdir logs

echo [%date% %time%] Starting sync of AR Invoices and Credit Memos... >> "%LOG_FILE%"

REM Activate virtual environment if you have one
REM call venv\Scripts\activate

REM Run the sync command using pythonw and redirect output to log file
pythonw manage.py sync_all_sales_data >> "%LOG_FILE%" 2>&1

echo [%date% %time%] Sync completed. >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"
