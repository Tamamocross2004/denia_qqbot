@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Watchdog: reboots NapCat when the bot stays offline too long.
rem Uses the heartbeat bot.py writes into qqbot\logs\heartbeat.json.
rem Pause it any time by creating an empty file: qqbot\logs\watchdog.pause

if not exist "qqbot\.venv\Scripts\python.exe" (
    echo [!] Dependencies not installed yet. Run qqbot\start.bat once first.
    pause
    exit /b 1
)

echo [*] Watchdog starting ...
echo     Settings live in qqbot\config.json (watchdog section)
echo     To pause it:  create an empty file  qqbot\logs\watchdog.pause
echo     To resume:    delete that file
echo.

"qqbot\.venv\Scripts\python.exe" "qqbot\tools\watchdog.py"
pause
exit /b 0
