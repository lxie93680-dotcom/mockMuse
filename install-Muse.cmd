@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\muse-windows.ps1" -Action install %*
set "muse_exit=%ERRORLEVEL%"
if not "%muse_exit%"=="0" pause
exit /b %muse_exit%
