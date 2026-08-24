@echo off
rem === Flow Engine - lanceur tout-en-un (sans fenetre parasite) ===
cd /d "%~dp0"

rem -- serveur deja actif sur le port 8000 ? --
netstat -an | find ":8000" | find "LISTENING" >nul
if not errorlevel 1 goto ouvrir

rem -- demarrage SANS fenetre : pythonw n'affiche aucune console --
set "PYW=pythonw.exe"
where pythonw.exe >nul 2>&1 || set "PYW=py -w"
start "" %PYW% "%~dp0flow_dashboard.py"

rem -- attendre que le serveur reponde vraiment (max 20s) --
for /l %%i in (1,1,20) do (
    timeout /t 1 >nul
    netstat -an | find ":8000" | find "LISTENING" >nul && goto ouvrir
)

:ouvrir
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if exist "%CHROME%" (
    rem --app = fenetre dediee sans barre d'adresse
    rem --user-data-dir = profil separe -> Windows groupe la fenetre sous NOTRE icone
    start "" "%CHROME%" --app=http://localhost:8000 --window-size=1600,1000 --user-data-dir="%LocalAppData%\FlowEngineApp"
) else (
    start "" http://localhost:8000
)
exit
