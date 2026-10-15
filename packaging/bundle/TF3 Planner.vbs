' TF3 Planner (built app): start TF3Planner.exe from the folder above _internal.
' Used by the desktop shortcut that the control center creates (tf3planner/control/shortcut.py).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
exe = fso.BuildPath(fso.GetParentFolderName(here), "TF3Planner.exe")
If Not fso.FileExists(exe) Then
  MsgBox "TF3Planner.exe not found: " & exe, 16, "TF3 Planner"
  WScript.Quit 1
End If
args = ""
For Each a In WScript.Arguments
  args = args & " """ & a & """"
Next
sh.Run """" & exe & """" & args, 1, False
