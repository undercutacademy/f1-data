' Runs ingest.bat with no visible window (used by Task Scheduler).
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & shell.CurrentDirectory & "\ingest.bat"" --max-wait 0", 0, False
