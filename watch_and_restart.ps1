# Surveille flow_engine.py / flow_dashboard.py et redemarre le serveur des qu'un
# changement est detecte. Fait aussi le lancement initial si rien ne tourne encore.

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$files = @("$dir\flow_engine.py", "$dir\flow_dashboard.py")
$port = 8000

function Get-ServerPids {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
}

function Stop-Server {
    $pids = Get-ServerPids
    foreach ($p in $pids) {
        try { Stop-Process -Id $p -Force -ErrorAction Stop; Write-Host "  [watch] arret PID $p" } catch {}
    }
    if ($pids) { Start-Sleep -Milliseconds 800 }
}

function Start-Server {
    $pyw = "pythonw.exe"
    if (-not (Get-Command $pyw -ErrorAction SilentlyContinue)) { $pyw = "py" }
    Start-Process -FilePath $pyw -ArgumentList "`"$dir\flow_dashboard.py`"" -WorkingDirectory $dir -WindowStyle Hidden
    Write-Host "  [watch] serveur relance ($(Get-Date -Format 'HH:mm:ss'))"
}

# etat initial des dates de modification
$lastWrite = @{}
foreach ($f in $files) { $lastWrite[$f] = (Get-Item $f).LastWriteTimeUtc }

if (-not (Get-ServerPids)) {
    Write-Host "  [watch] aucun serveur actif -> lancement initial"
    Start-Server
}

Write-Host "  [watch] surveillance active sur : $($files -join ', ')"

while ($true) {
    Start-Sleep -Seconds 2
    $changed = $false
    foreach ($f in $files) {
        $cur = (Get-Item $f -ErrorAction SilentlyContinue).LastWriteTimeUtc
        if ($cur -and $cur -ne $lastWrite[$f]) {
            $lastWrite[$f] = $cur
            $changed = $true
        }
    }
    if ($changed) {
        Write-Host "  [watch] changement detecte -> redemarrage"
        Stop-Server
        Start-Server
    }
}
