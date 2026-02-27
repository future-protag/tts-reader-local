@echo off
:: Check if running as Administrator
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "d:\Agent work\tts-reader-local"
py -3.12 tts_reader.py
pause
