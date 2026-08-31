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
rem -- onglet Chrome NORMAL (barre d'adresse, favoris, etc.) sur Flow Engine, EN PLUS
rem    de la fenetre app ci-dessous -- passe par le handler d'URL par defaut de Windows,
rem    s'ajoute en onglet dans une fenetre Chrome deja ouverte sans forcer de fenetre --
start "" "http://localhost:8000"

timeout /t 2 >nul

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

rem -- app Chrome installee (Chrome > Installer Flow Engine...) : seule methode qui
rem    garde la bonne icone quand on epingle la fenetre a la barre des taches.
rem    --silent-launch = empeche en plus l'ouverture redondante de la fenetre de
rem    demarrage normale du profil "Default" (deja couverte par Google ci-dessus).
set "CHROMEPROXY=%ProgramFiles%\Google\Chrome\Application\chrome_proxy.exe"
if not exist "%CHROMEPROXY%" set "CHROMEPROXY=%ProgramFiles(x86)%\Google\Chrome\Application\chrome_proxy.exe"
if not exist "%CHROMEPROXY%" set "CHROMEPROXY=%LocalAppData%\Google\Chrome\Application\chrome_proxy.exe"

if exist "%CHROMEPROXY%" (
    start "" "%CHROMEPROXY%" --profile-directory=Default --app-id=fkjkjjjmlkjnchecijdkfolgdhilkejl --silent-launch
) else (
    rem -- repli si l'app installee a disparu (desinstallee/reinstallee) --
    if exist "%CHROME%" (
        start "" "%CHROME%" --app=http://localhost:8000 --window-size=1600,1000 --user-data-dir="%LocalAppData%\FlowEngineApp"
    ) else (
        start "" http://localhost:8000
    )
)
exit
