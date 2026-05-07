@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-and-start-kern-tauri.ps1" -NoStart %*
exit /b %ERRORLEVEL%
