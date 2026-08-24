# Cree un raccourci "Flow Engine" (avec icone) sur le Bureau.
# A lancer UNE SEULE FOIS, depuis le dossier du projet :  .\Creer_raccourci.ps1
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $dir "Flow.bat"
$ico = Join-Path $dir "flow.ico"

if (-not (Test-Path $bat)) { Write-Host "Flow.bat introuvable dans $dir" -ForegroundColor Red; exit }

$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Flow Engine.lnk"
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut($lnk)
$s.TargetPath       = $bat
$s.WorkingDirectory = $dir
$s.WindowStyle      = 7           # demarre reduit (la fenetre noire ne clignote pas)
$s.Description      = "Flow Engine - dashboard options"
if (Test-Path $ico) { $s.IconLocation = $ico }
$s.Save()

Write-Host "Raccourci cree : $lnk" -ForegroundColor Green
Write-Host ""
Write-Host "Pour l'epingler a la barre des taches :" -ForegroundColor Cyan
Write-Host "  clic droit sur l'icone du Bureau  ->  Epingler a la barre des taches"
Write-Host "  (sur Windows 11 : Afficher plus d'options -> Epingler a la barre des taches)"
