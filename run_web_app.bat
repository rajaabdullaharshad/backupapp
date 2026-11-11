@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python src\frappe_auto_deploy\web_main.py
    exit /b %errorlevel%
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 src\frappe_auto_deploy\web_main.py
    exit /b %errorlevel%
)

echo Python not found. Install Python 3 and try again.
exit /b 1
