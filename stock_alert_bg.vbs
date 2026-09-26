' Run stock_alert.bat with no window. Log: stock_alert.log / stop: stock_alert_stop.bat
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "cmd /c """ & dir & "\stock_alert.bat""", 0, False
