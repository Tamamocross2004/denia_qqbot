@echo off
chcp 65001 >nul
title Denia Voice API
cd /d "%~dp0tts\GPT-SoVITS"

rem Point ffmpeg at the static build shipped with imageio-ffmpeg.
rem The system has no ffmpeg on PATH, and Windows child processes
rem cannot see a POSIX-style PATH, so an absolute path is required.
set "FFMPEG_BIN=%~dp0tts\bin\ffmpeg.exe"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"

if not exist "%~dp0tts\venv310\Scripts\python.exe" (
    echo [!] venv310 not found - run the setup steps in README first.
    pause
    exit /b 1
)

if not exist "%FFMPEG_BIN%" (
    echo [!] ffmpeg.exe missing at %FFMPEG_BIN%
    pause
    exit /b 1
)

echo [*] Model  : SoVITS_weights_v2\daniya_e164.pth
echo [*] Device : cuda (rtx 4070, about 1.7 GB vram)
echo [*] Listen : http://127.0.0.1:9880
echo [*] Loading takes ~60s. Keep this window open while the bot runs.
echo.

"%~dp0tts\venv310\Scripts\python.exe" api_v2.py -c GPT_SoVITS\configs\tts_infer.yaml -a 127.0.0.1 -p 9880

echo.
echo [!] Voice API stopped.
pause
