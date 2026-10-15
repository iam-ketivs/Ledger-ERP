@echo off
cd /d "%~dp0"
echo ============================================================
echo   Checking why the tray icon is missing
echo ============================================================
echo.
if not exist venv\Scripts\activate (
    echo No build environment found. Run build_exe.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate
python --version
echo.
echo Is pystray installed?
python -c "import pystray; print('  YES - pystray', pystray.__version__)" 2>nul || echo   NO - pystray is NOT installed
echo.
echo Is Pillow installed?
python -c "import PIL; print('  YES - Pillow', PIL.__version__)" 2>nul || echo   NO - Pillow is NOT installed
echo.
echo Can the tray backend load?
python -c "import pystray._win32; print('  YES - Windows tray backend loads fine')" 2>nul || echo   NO - the Windows tray backend failed to load
echo.
echo ============================================================
echo   If any say NO, that is why there was no icon.
echo   It does not matter now - closing the Ledger window quits
echo   the app, so the tray icon is optional.
echo ============================================================
pause
