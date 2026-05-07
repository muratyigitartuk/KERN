@echo off
setlocal

call "%~dp0install-kern.cmd" %*
exit /b %ERRORLEVEL%
