@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title MusicScope Audio Analyzer

set "PYTHON_CMD="
where python.exe >nul 2>&1
if errorlevel 1 goto try_py_launcher
python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto try_py_launcher
set "PYTHON_CMD=python.exe"
goto start_app

:try_py_launcher
where py.exe >nul 2>&1
if errorlevel 1 goto python_missing
py.exe -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto python_too_old
set "PYTHON_CMD=py.exe -3"
goto start_app

:python_missing
echo [ERROR] Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/
echo During installation, enable "Add Python to PATH".
pause
exit /b 1

:python_too_old
echo [ERROR] MusicScope requires Python 3.10 or newer.
py.exe -3 --version
pause
exit /b 1

:start_app
echo Starting MusicScope...
echo The browser will open automatically.
echo Close this window or press Ctrl+C to stop the server.
%PYTHON_CMD% app.py
set "APP_EXIT=%ERRORLEVEL%"
if "%APP_EXIT%"=="0" exit /b 0
echo.
echo [ERROR] MusicScope exited unexpectedly with code %APP_EXIT%.
echo Please keep this window open and capture the error above.
pause
exit /b %APP_EXIT%

