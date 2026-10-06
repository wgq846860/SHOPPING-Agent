@echo off
chcp 65001 >nul
title Agentic Shopping Demo Launcher
cd /d "%~dp0"
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo [launcher] .venv not found. Create it first, see README.md:
    echo   %LOCALAPPDATA%\Programs\Python\Python314\python.exe -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m scripts.launcher
if errorlevel 1 pause
