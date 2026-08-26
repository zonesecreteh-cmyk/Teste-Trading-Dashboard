@echo off
rem Arrete le serveur Flow Engine.
rem IMPORTANT : on tue les DEUX formes de Python. Le raccourci lance pythonw.exe
rem (sans fenetre) mais un demarrage manuel par "py flow_dashboard.py" cree un
rem python.exe. Ne tuer que pythonw laissait un ancien serveur vivant sur le port
rem 8000, qui continuait a servir du code perime malgre les mises a jour.
taskkill /F /IM pythonw.exe >nul 2>&1
taskkill /F /IM python.exe  >nul 2>&1
echo Serveur Flow Engine arrete (python.exe et pythonw.exe).
timeout /t 2 >nul
