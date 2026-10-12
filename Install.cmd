@echo off
rem Double-click to install Surround Mixer (add  -Startup  to start it when you log in,
rem or run  Install.cmd -Uninstall  to remove it).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
