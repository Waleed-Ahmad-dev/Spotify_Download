@echo off
REM ============================================================================
REM  Spotify -> Audio Downloader  -  Windows launcher
REM  Double-click this file, or run it from a terminal (it forwards any flags).
REM ============================================================================
setlocal
cd /d "%~dp0"

REM ---- Locate Python (prefer the 'py' launcher, then 'python') ----------------
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo [ERROR] Python was not found on your PATH.
    echo         Install Python 3.8+ from https://www.python.org/downloads/
    echo         ^(tick "Add python.exe to PATH" during setup^), then re-run this.
    echo.
    pause
    exit /b 1
)

REM ---- Run the app, forwarding any command-line arguments ---------------------
%PY% main.py %*
set "RC=%ERRORLEVEL%"

REM ---- If double-clicked (no args), keep the window open so output is readable -
if "%~1"=="" pause
endlocal & exit /b %RC%
