@echo off
rem Collects NGS logs, crash dumps and Windows fault events into NGS-crash-*.zip beside NGS.exe.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Collect-NgsCrash.ps1"
pause
