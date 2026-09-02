@echo off
REM Windows batch script to keep the fourth, separate submittal-only mailbox
REM watcher running. Mirrors watch_project.bat exactly, just for the
REM SUBMITTAL_IMAP_* mailbox (see emailagent/management/commands/watch_submittal.py).
REM watch_submittal is a long-running process (IMAP IDLE) that is not meant to
REM exit on its own -- this loop restarts it a few seconds after any exit
REM (crash, forced kill, machine coming back from sleep, etc.) so Task
REM Scheduler only needs a single "At log on" trigger, no XML-only
REM "restart on failure" setting required.
REM Uses pythonw to run without showing a console window.
REM Logs are written by the command itself to logs\watch_submittal.log; this
REM launcher's own start/stop lines go to logs\watch_submittal_launcher.log.

REM Change to the directory where this batch file is located (should be salesorder/)
cd /d %~dp0

if not exist "logs" mkdir logs

:loop
echo [%date% %time%] Starting Submittal mailbox watcher (watch_submittal)... >> logs\watch_submittal_launcher.log

pythonw manage.py watch_submittal >> logs\watch_submittal_launcher.log 2>&1

echo [%date% %time%] Submittal mailbox watcher exited -- restarting in 10s. >> logs\watch_submittal_launcher.log
timeout /t 10 /nobreak >nul
goto loop
