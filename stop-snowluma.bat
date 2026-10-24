@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo  Stop SnowLuma, hand the account back to NapCat
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0snowluma\stop.ps1"
echo.
pause
