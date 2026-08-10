@echo off
setlocal EnableExtensions

cd /d "%~dp0"
chcp 65001 >nul
title Arena Hero Agent

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python virtual environment was not found:
    echo         %PYTHON_EXE%
    echo Create .venv and install requirements.txt first.
    pause
    exit /b 1
)

if not exist "%~dp0arena_agent.py" (
    echo [ERROR] arena_agent.py was not found in:
    echo         %~dp0
    pause
    exit /b 1
)

if not exist "%~dp0.env" if not defined ARENA_HERO_API_KEY (
    echo [ERROR] .env was not found and ARENA_HERO_API_KEY is not set.
    echo Copy .env.example to .env and add your Agent token.
    pause
    exit /b 2
)

echo Starting Arena Hero Agent...
echo Press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" "%~dp0arena_agent.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%" == "0" (
    echo.
    echo Agent exited with code %EXIT_CODE%.
    pause
)

endlocal & exit /b %EXIT_CODE%
