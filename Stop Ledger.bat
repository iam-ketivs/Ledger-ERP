@echo off
echo Stopping Ledger...
taskkill /IM Ledger.exe /F >nul 2>&1
if errorlevel 1 (
    echo Ledger was not running.
) else (
    echo Ledger stopped.
)
timeout /t 2 >nul
