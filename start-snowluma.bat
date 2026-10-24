@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo  SnowLuma trial launcher (replaces NapCat)
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0snowluma\launch.ps1"
echo.
pause
