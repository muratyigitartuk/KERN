@echo off
setlocal

call "%~dp0start-kern.cmd" %*
exit /b %ERRORLEVEL%
