@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title MusicScope Automated Tests

set "PYTHON_CMD="
where python.exe >nul 2>&1
if errorlevel 1 goto try_py_launcher
python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto try_py_launcher
set "PYTHON_CMD=python.exe"
goto run_tests

:try_py_launcher
where py.exe >nul 2>&1
if errorlevel 1 goto python_missing
py.exe -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto python_missing
set "PYTHON_CMD=py.exe -3"
goto run_tests

:python_missing
echo [ERROR] Python 3.10 or newer was not found.
pause
exit /b 1

:run_tests
echo Running MusicScope automated tests...
%PYTHON_CMD% -W error::ResourceWarning -m unittest discover -s tests -v
set "TEST_EXIT=%ERRORLEVEL%"
echo.
if not "%TEST_EXIT%"=="0" goto tests_failed
echo [RESULT] All tests passed.
pause
exit /b 0

:tests_failed
echo [RESULT] One or more tests failed, exit code %TEST_EXIT%.
pause
exit /b %TEST_EXIT%

