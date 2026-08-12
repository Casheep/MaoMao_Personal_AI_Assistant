@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-beta-packages.ps1" -Edition lite
if errorlevel 1 goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-beta-packages.ps1" -Edition full
if errorlevel 1 goto failed
echo.
echo Lite and Full packages are ready in the dist folder.
pause
exit /b 0

:failed
echo.
echo Build failed. Read the message above.
pause
exit /b 1
