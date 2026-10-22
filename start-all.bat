@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "D:\QQ.exe" (
    echo [!] QQ.exe not found at D:\QQ.exe
    echo     Edit QQEXE in napcat\run.bat if your path differs.
    pause
    exit /b 1
)

if not exist "qqbot\.venv\Scripts\python.exe" (
    echo [!] Dependencies not installed yet. Run qqbot\start.bat once first.
    pause
    exit /b 1
)

echo [*] Starting QQ Bot ...
start "QQ Bot" cmd /c "qqbot\start.bat"

"%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul

rem Delete the old QR first, so we can tell whether this run produced a new one.
rem With quick login enabled a QR usually never appears - that is the good case.
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
echo  2. Watch the "QQ Bot" window for: NapCat connected
echo  3. Bot log: qqbot\logs\bot.log
echo ==========================================================
echo.
pause
exit /b 0
