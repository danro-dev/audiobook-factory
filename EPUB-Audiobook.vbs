' EPUB a Audiobook - launcher sin ventana de consola
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
cmd = Chr(34) & dir & "\.venv\Scripts\pythonw.exe" & Chr(34) & " -m epub2audiobook"
sh.Run cmd, 0, False
