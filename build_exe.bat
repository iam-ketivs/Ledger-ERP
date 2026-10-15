@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Building Ledger.exe
echo   Needs the internet ONCE for the build tools.
echo   The finished Ledger.exe then runs completely offline.
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed, or not on your PATH.
    echo Install it from python.org and tick "Add Python to PATH".
    pause
    exit /b 1
)

python --version
echo.

REM Windows locks a running .exe, so PyInstaller cannot replace it. Close any copy
REM that is already running before building, otherwise the build dies at the last
REM step with "Access is denied".
tasklist /FI "IMAGENAME eq Ledger.exe" 2>nul | find /I "Ledger.exe" >nul
if not errorlevel 1 (
    echo Ledger is currently running - closing it so it can be replaced...
    taskkill /IM Ledger.exe /F >nul 2>&1
    timeout /t 2 >nul
    echo Closed.
    echo.
)

if not exist venv (
    echo Creating build environment...
    python -m venv venv
)
call venv\Scripts\activate

python -m pip install --upgrade pip setuptools wheel >nul 2>&1

echo [1/3] Installing what the app needs...
pip install --only-binary=:all: --upgrade -r requirements-core.txt
if errorlevel 1 (
    echo.
    echo ERROR: Could not install Flask/waitress. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo [2/3] Installing the tray icon (optional)...
set TRAY=1
pip install --only-binary=:all: --upgrade pystray Pillow
if errorlevel 1 (
    set TRAY=0
    echo.
    echo   NOTE: the tray icon could not be installed - carrying on without it.
    echo   Ledger will still work. Use "Stop Ledger.bat" to close it.
    echo.
)

echo.
echo [3/3] Installing the builder...
pip install --only-binary=:all: --upgrade pyinstaller
if errorlevel 1 (
    echo ERROR: Could not install PyInstaller.
    pause
    exit /b 1
)

echo.
echo Building... this takes a minute or two.
pyinstaller Ledger.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo BUILD FAILED - see the messages above.
    echo.
    echo If it says "Access is denied" on Ledger.exe, it is still running.
    echo Close it ^(tray icon, or "Stop Ledger.bat", or Task Manager^) and run this again.
    pause
    exit /b 1
)

if exist "Stop Ledger.bat" copy /Y "Stop Ledger.bat" "dist\Stop Ledger.bat" >nul

if exist instance\erp.db (
    if not exist dist\instance\erp.db (
        echo.
        echo Copying your existing database into dist...
        if not exist dist\instance mkdir dist\instance
        copy /Y instance\erp.db dist\instance\erp.db >nul
        if exist instance\secret.key copy /Y instance\secret.key dist\instance\secret.key >nul
        echo Copied.
    )
)

echo.
echo ============================================================
echo   Done.  Your app is:  dist\Ledger.exe
if "!TRAY!"=="0" (
echo   Built WITHOUT the tray icon - use dist\Stop Ledger.bat to quit.
) else (
echo   Tray icon included - right-click it by the clock to quit.
)
echo.
echo   Right-click dist\Ledger.exe and Pin to taskbar.
echo ============================================================
echo.
pause
