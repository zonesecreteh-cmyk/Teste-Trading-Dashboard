@echo off
rem Arrete le serveur Flow Engine (utile car pythonw tourne sans fenetre visible)
taskkill /F /IM pythonw.exe >nul 2>&1
echo Serveur Flow Engine arrete.
timeout /t 2 >nul
