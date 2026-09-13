# Surveille flow_engine.py / flow_dashboard.py et redemarre le serveur des qu'un
# changement est detecte. Fait aussi le lancement initial si rien ne tourne encore.
#
# Robustesse (suite a 2 arrets constates sans laisser de trace) :
#   - Tout est journalise dans flow_watch_log.txt (horodate), plus de boite noire.
#   - Le corps de la boucle est protege par try/catch : une erreur inattendue est
#     journalisee et la boucle continue, elle ne tue plus le process.
#   - Ce script est cense etre lance par la tache planifiee Windows "Flow watch"
#     (declencheur "a l'ouverture de session", + redemarrage auto si le process
#     s'arrete quand meme) -- un double-clic manuel ne survit jamais a un reboot.
#
# Double-bind (2026-09-13) : ThreadingHTTPServer active allow_reuse_address, ce qui
# permet a Windows de laisser DEUX process ecouter le meme port en cas de redemarrage
# rapproche (l'ancien pas encore vraiment mort, le nouveau deja lie) -- resultat : les
# requetes tombent au hasard sur l'ancienne ou la nouvelle version du code, ce qui a
# fait croire pendant des heures a des modifs "non prises en compte". Apres chaque
# Start-Server, Confirm-SingleListener verifie que SEUL le PID qu'on vient de lancer
# ecoute sur le port, et tue immediatement tout autre PID trouve (en le journalisant).

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$files = @("$dir\flow_engine.py", "$dir\flow_dashboard.py")
$port = 8000
$logPath = "$dir\flow_watch_log.txt"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    try { Add-Content -Path $logPath -Value $line -Encoding utf8 } catch {}
}

# rotation simple : si le journal depasse ~2 Mo, on ne garde que la fin (evite une
# croissance sans fin sur un script cense tourner des semaines)
try {
    if ((Test-Path $logPath) -and (Get-Item $logPath).Length -gt 2MB) {
        $tail = Get-Content $logPath -Tail 2000
        Set-Content -Path $logPath -Value $tail -Encoding utf8
    }
} catch {}

function Get-ServerPids {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
}

function Stop-Server {
    $pids = Get-ServerPids
    foreach ($p in $pids) {
        try { Stop-Process -Id $p -Force -ErrorAction Stop; Log "arret PID $p" } catch {}
    }
    if ($pids) { Start-Sleep -Milliseconds 800 }
}

function Start-Server {
    $pyw = "pythonw.exe"
    if (-not (Get-Command $pyw -ErrorAction SilentlyContinue)) { $pyw = "py" }
    $p = Start-Process -FilePath $pyw -ArgumentList "`"$dir\flow_dashboard.py`"" -WorkingDirectory $dir -WindowStyle Hidden -PassThru
    Log "serveur relance (PID $($p.Id))"
    return $p.Id
}

# Attend que le PID qu'on vient de lancer apparaisse comme listener sur le port
# (jusqu'a ~5s), puis tue tout AUTRE PID trouve en ecoute sur ce meme port -- c'est
# le double-bind Windows documente en tete de fichier (allow_reuse_address laisse
# l'ancien process repondre encore pendant que le nouveau est deja lie). Journalise
# chaque PID perime tue pour que ce ne soit plus une boite noire.
function Confirm-SingleListener($pidAttendu) {
    $tentatives = 0
    while ($tentatives -lt 25) {
        if ((Get-ServerPids) -contains $pidAttendu) { break }
        Start-Sleep -Milliseconds 200
        $tentatives++
    }
    $perimes = Get-ServerPids | Where-Object { $_ -ne $pidAttendu }
    foreach ($p in $perimes) {
        try {
            Stop-Process -Id $p -Force -ErrorAction Stop
            Log "PID PERIME encore en ecoute sur le port $port apres redemarrage -> tue (PID $p, attendu PID $pidAttendu)"
        } catch {
            Log "PID PERIME detecte (PID $p) mais echec de l'arret : $($_.Exception.Message)"
        }
    }
}

Log "=== demarrage du watcher (PID $PID) ==="

# etat initial des dates de modification
$lastWrite = @{}
foreach ($f in $files) { $lastWrite[$f] = (Get-Item $f).LastWriteTimeUtc }

if (-not (Get-ServerPids)) {
    Log "aucun serveur actif -> lancement initial"
    $nouveauPid = Start-Server
    Confirm-SingleListener $nouveauPid
} else {
    # un serveur ecoute deja (ex : watcher relance apres un crash du watcher lui-meme,
    # le serveur survit) -- on verifie quand meme qu'il n'y en a pas un second en trop.
    # On garde le PID le plus RECENT (le plus probable d'avoir le code a jour) et on
    # tue les autres.
    $existants = Get-ServerPids
    if ($existants.Count -gt 1) {
        Log "plusieurs PID deja en ecoute au demarrage du watcher -> nettoyage"
        $plusRecent = $existants | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue } |
            Sort-Object StartTime -Descending | Select-Object -First 1 -ExpandProperty Id
        if ($plusRecent) { Confirm-SingleListener $plusRecent }
    }
}

Log "surveillance active sur : $($files -join ', ')"

while ($true) {
    try {
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
            Log "changement detecte -> redemarrage"
            Stop-Server
            $nouveauPid = Start-Server
            Confirm-SingleListener $nouveauPid
        }
    } catch {
        # une erreur inattendue ne doit JAMAIS tuer la boucle -- on la journalise
        # et on continue (protege aussi la tache planifiee d'un arret silencieux)
        Log "ERREUR (ignoree, boucle continue) : $($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
}
