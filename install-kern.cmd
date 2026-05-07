@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-kern.ps1" %*
exit /b %ERRORLEVEL%
