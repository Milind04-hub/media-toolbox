@echo off
cd /d "%~dp0"
rem Uses the Windows "py" launcher. Edit this line if your Python is elsewhere.
py app.py || python app.py
pause
