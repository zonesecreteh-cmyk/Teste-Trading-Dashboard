' Lance run_footprint.bat sans fenetre de console visible -- evite qu'une fenetre
' noire apparaisse par-dessus les autres applications a chaque declenchement
' de la tache planifiee "Flow footprint" (toutes les 2h). Attend la fin et
' transmet le code de sortie pour que le Planificateur de taches voie un echec eventuel.
Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
intReturn = objShell.Run("""" & strDir & "\run_footprint.bat""", 0, True)
WScript.Quit intReturn
