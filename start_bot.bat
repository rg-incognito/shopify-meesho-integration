@echo off
rem Starts the Telegram bot (which also runs scheduled push + daily resync).
rem Restarts it automatically if it crashes.
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" bot.py
echo Bot exited, restarting in 15 seconds...
timeout /t 15 /nobreak >nul
goto loop
