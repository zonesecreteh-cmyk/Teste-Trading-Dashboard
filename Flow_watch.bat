@echo off
rem === Flow Engine - surveillance + redemarrage automatique ===
rem Regarde flow_engine.py et flow_dashboard.py : des qu'un des deux change (date de
rem modification), tue le serveur et le relance. Laisse cette fenetre ouverte pendant
rem que tu codes/qu'on modifie le dashboard -- plus besoin de relancer a la main.
cd /d "%~dp0"
rem Toute sortie (normale ou erreur) va aussi dans flow_watch_log.txt via Log()
rem dans le script PS1 lui-meme -- pas de "pause" ici : bloquerait pour toujours
rem une tache planifiee lancee sans personne pour appuyer sur une touche.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watch_and_restart.ps1"
