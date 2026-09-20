' Lance Flow_watch.bat sans fenetre de console visible. Le watcher qu'il demarre
' boucle indefiniment (surveillance des .py + redemarrage du serveur) -- sans ce
' lanceur cache, la fenetre cmd.exe qui l'heberge restait ouverte en permanence des
' l'ouverture de session (tache planifiee "Flow watch"), meme si le serveur qu'elle
' surveille est lui deja invisible (pythonw.exe). Corrige le 2026-09-20.
Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
intReturn = objShell.Run("""" & strDir & "\Flow_watch.bat""", 0, True)
WScript.Quit intReturn
