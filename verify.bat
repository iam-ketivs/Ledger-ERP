@echo off
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo Python not found on PATH - cannot run the check.
    pause
    exit /b 1
)
python verify.py
pause
