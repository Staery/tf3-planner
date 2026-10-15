@echo off
rem Creates the "TF3 Planner" shortcut on the desktop.
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 -m tf3planner.control.shortcut
pause
