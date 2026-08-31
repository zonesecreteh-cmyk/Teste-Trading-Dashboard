@echo off
rem === Flow Engine - surveillance + redemarrage automatique ===
rem Regarde flow_engine.py et flow_dashboard.py : des qu'un des deux change (date de
rem modification), tue le serveur et le relance. Laisse cette fenetre ouverte pendant
rem que tu codes/qu'on modifie le dashboard -- plus besoin de relancer a la main.
cd /d "%~dp0"
echo Surveillance de flow_engine.py et flow_dashboard.py (Ctrl+C pour arreter)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watch_and_restart.ps1"
echo.
echo [Le script s'est arrete -- erreur eventuelle affichee ci-dessus]
pause
