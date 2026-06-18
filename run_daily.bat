@echo off
cd /d "%~dp0"
py daily.py >> daily_log.txt 2>&1
py sync_github.py >> daily_log.txt 2>&1
