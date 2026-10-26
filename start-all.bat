@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem start-all.bat            -> SnowLuma (default since 2026-10-26)
rem start-all.bat napcat     -> the old NapCat path
rem Both: one Bot window + one Watchdog window, then the protocol side.

if not exist "qqbot\.venv\Scripts\python.exe" (
    echo [!] Dependencies not installed yet. Run qqbot\start.bat once first.
    pause
    exit /b 1
)

if /i "%~1"=="napcat" goto napcat
goto snowluma

:snowluma
if not exist "snowluma\node.exe" (
    echo [!] snowluma\ not found. See README "SnowLuma".
    pause
    exit /b 1
)
echo [*] Starting QQ Bot ...
start "QQ Bot" cmd /c "qqbot\start.bat"
"%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul
start "Watchdog" cmd /c "start-watchdog.bat"
echo [*] Starting SnowLuma ...
echo     kills NapCat, restarts the bot QQ, hooks it, then returns here.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0snowluma\launch.ps1"
echo.
echo ==========================================================
echo  If the QQ window asks for a login, scan it with the BOT
echo  account. Then both windows should settle on their own.
echo  WebUI : http://127.0.0.1:5099
echo  Pass  : snowluma\logs\webui-credentials.txt
echo  Bot   : qqbot\logs\bot.log    Watchdog: qqbot\logs\watchdog.log
echo ==========================================================
echo.
pause
exit /b 0

:napcat
if not exist "D:\QQ.exe" (
    echo [!] QQ.exe not found at D:\QQ.exe
    echo     Edit QQEXE in napcat\run.bat if your path differs.
    pause
    exit /b 1
)
echo [*] Starting QQ Bot ...
start "QQ Bot" cmd /c "qqbot\start.bat"
"%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul
start "Watchdog" cmd /c "start-watchdog.bat"

rem Delete the old QR first, so we can tell whether this run produced a new one.
if exist "napcat\cache\qrcode.png" del /q "napcat\cache\qrcode.png" >nul 2>&1
if exist "napcat\cache\qrcode_big.png" del /q "napcat\cache\qrcode_big.png" >nul 2>&1

echo [*] Starting NapCat ...
start "NapCat" cmd /c "napcat\run.bat"

echo [*] Waiting 15s ...
"%SystemRoot%\System32\ping.exe" -n 16 127.0.0.1 >nul

if exist "napcat\cache\qrcode.png" (
    echo [*] A QR code was generated - quick login did not work this time.
    "qqbot\.venv\Scripts\python.exe" "qqbot\tools\qr_big.py"
    if exist "napcat\cache\qrcode_big.png" start "" "napcat\cache\qrcode_big.png"
) else (
    echo [*] No QR code - quick login worked, account is already logging in.
)

echo.
echo ==========================================================
echo  1. If a QR image popped up, scan it with the BOT account
echo  2. Watch the "QQ Bot" window for: connected
echo  3. Bot log: qqbot\logs\bot.log
echo ==========================================================
echo.
pause
exit /b 0
