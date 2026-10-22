@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [*] First run: creating venv ...
    python -m venv .venv
    if errorlevel 1 goto :fail

    if exist "..\bin\uv.exe" (
        echo [*] Installing deps via uv ...
        "..\bin\uv.exe" pip install --python ".venv\Scripts\python.exe" ^
            --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
    ) else (
        echo [*] Installing deps via pip ...
        ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    )
    if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" bot.py
echo.
echo [!] Bot stopped.
pause
exit /b 0

:fail
echo [!] Setup failed.
pause
exit /b 1
