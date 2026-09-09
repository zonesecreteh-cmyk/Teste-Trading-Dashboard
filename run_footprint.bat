@echo off
cd /d "%~dp0"
py collect_footprint.py >> footprint_log.txt 2>&1
