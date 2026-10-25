@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0NGS.exe" (
    start "" "%~dp0NGS.exe" %*
    exit /b
)
python -B "%~dp0launcher.py" %*
if errorlevel 1 pause
