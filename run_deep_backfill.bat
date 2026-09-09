@echo off
cd /d "%~dp0"
py -u deep_backfill.py >> deep_backfill_log.txt 2>&1
