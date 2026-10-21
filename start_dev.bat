@echo off
setlocal
title GetLiveAward - Development

pushd "%~dp0"
if errorlevel 1 goto directory_error

set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "DEV_PYTHON=%CD%\.venv\Scripts\python.exe"

if exist "%DEV_PYTHON%" goto check_environment

echo [Dev] Creating .venv...
py -3 -c "import sys; sys.exit(not sys.version_info >= (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_with_launcher
python -c "import sys; sys.exit(not sys.version_info >= (3, 10))" >nul 2>&1
if errorlevel 1 goto python_error
python -m venv .venv
if errorlevel 1 goto failed
goto install_dependencies

:create_with_launcher
py -3 -m venv .venv
if errorlevel 1 goto failed
goto install_dependencies

:check_environment
"%DEV_PYTHON%" -c "import sys; sys.exit(not sys.version_info >= (3, 10))"
if errorlevel 1 goto python_error
"%DEV_PYTHON%" -c "import PyQt5.QtCore, requests, qrcode, PIL" >nul 2>&1
if not errorlevel 1 goto run_app

:install_dependencies
echo [Dev] Installing requirements...
"%DEV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:run_app
echo [Dev] Starting app.py from source. Close the app to return here.
"%DEV_PYTHON%" app.py
if errorlevel 1 goto failed
popd
exit /b 0

:python_error
echo [Error] Python 3.10 or newer is required. Python 3.12 is recommended.
echo [Error] If .venv already exists, recreate it with a supported Python version.
goto failed

:directory_error
echo [Error] Cannot open the project directory.
pause
exit /b 1

:failed
echo.
echo [Error] Development startup failed. See the output above.
pause
popd
exit /b 1
