@echo off
rem TF3 Planner: one click - starts the control center in the background and opens the browser.
cd /d "%~dp0"
if exist "%~dp0TF3 Planner.vbs" (
  start "" wscript.exe "%~dp0TF3 Planner.vbs" %*
) else (
  start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 -m tf3planner.control.launcher %*
)
