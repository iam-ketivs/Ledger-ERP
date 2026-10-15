@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Building LedgerSetup.exe
echo.
echo   This produces ONE file to give a customer. They double-
echo   click it and Ledger installs itself - no Python, no
echo   folders to copy, no instructions.
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed, or not on your PATH.
    echo Get it from python.org and tick "Add Python to PATH".
    pause & exit /b 1
)

REM ---- find Inno Setup FIRST, before spending five minutes on a build we cannot use
echo [1/3] Looking for Inno Setup...
set "ISCC="

REM 1. the usual places
for %%P in (
  "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
  "%ProgramFiles%\Inno Setup 7\ISCC.exe"
  "%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 8\ISCC.exe"
  "%ProgramFiles%\Inno Setup 8\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
  "%ProgramFiles%\Inno Setup 5\ISCC.exe"
  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
  "%LOCALAPPDATA%\Programs\Inno Setup 5\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup\ISCC.exe"
  "%ProgramFiles%\Inno Setup\ISCC.exe"
  "C:\Inno Setup 6\ISCC.exe"
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) do if exist %%P set "ISCC=%%~P"

REM 1b. any "Inno Setup *" folder, whatever the version number
if not defined ISCC (
  for /d %%D in ("%ProgramFiles(x86)%\Inno Setup*") do if exist "%%D\ISCC.exe" set "ISCC=%%D\ISCC.exe"
)
if not defined ISCC (
  for /d %%D in ("%ProgramFiles%\Inno Setup*") do if exist "%%D\ISCC.exe" set "ISCC=%%D\ISCC.exe"
)
if not defined ISCC (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Inno Setup*") do if exist "%%D\ISCC.exe" set "ISCC=%%D\ISCC.exe"
)

REM 2. ask Windows
if not defined ISCC (
  for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do set "ISCC=%%I"
)

REM 3. ask the registry - Inno Setup records where it installed itself
if not defined ISCC (
  for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 7_is1" /v InstallLocation 2^>nul ^| find "InstallLocation"') do (
    if exist "%%B\ISCC.exe" set "ISCC=%%B\ISCC.exe"
  )
)
if not defined ISCC (
  for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 7_is1" /v InstallLocation 2^>nul ^| find "InstallLocation"') do (
    if exist "%%B\ISCC.exe" set "ISCC=%%B\ISCC.exe"
  )
)

REM 4. search the drive - slow, but it finds it wherever it is
if not defined ISCC (
  echo   Not in the usual places. Searching the drive, one moment...
  for /f "delims=" %%I in ('dir /s /b "%SystemDrive%\ISCC.exe" 2^>nul') do set "ISCC=%%I"
)

REM 5. give up guessing and just ask
if not defined ISCC (
  echo.
  echo   I cannot find ISCC.exe automatically.
  echo.
  echo   Find it yourself: right-click your Inno Setup shortcut,
  echo   choose "Open file location", and copy the folder path
  echo   from the address bar at the top.
  echo.
  set /p "ISCCDIR=Paste that folder here (or press Enter to stop): "
  if defined ISCCDIR (
    if exist "!ISCCDIR!\ISCC.exe" (
      set "ISCC=!ISCCDIR!\ISCC.exe"
    ) else if exist "!ISCCDIR!" (
      set "ISCC=!ISCCDIR!"
    )
  )
)
if not defined ISCC (
    echo.
    echo   Still cannot find it. Stopping here.
    pause & exit /b 1
)
REM Strip any stray quotes - %%P from the list above carries them, and quoting an
REM already-quoted path gives ""C:\...\ISCC.exe"" which Windows cannot run.
set ISCC=!ISCC:"=!
echo   Found: !ISCC!
echo.

REM ---- build the exe. CALL alone was not enough: build_exe.bat ends with PAUSE,
REM ---- which stopped the whole script here. Run it in its own window and wait.
echo [2/3] Building Ledger.exe (a few minutes)...
start /wait "Building Ledger" cmd /c "build_exe.bat"

if not exist "dist\Ledger.exe" (
    echo.
    echo   ERROR: dist\Ledger.exe was not produced.
    echo   Run build_exe.bat on its own and read what it says.
    pause & exit /b 1
)
echo   Ledger.exe is ready.
echo.

echo [3/3] Wrapping it into the installer...
"!ISCC!" installer.iss
if errorlevel 1 (
    echo.
    echo   The installer build failed. The message above says why.
    pause & exit /b 1
)

echo.
echo ============================================================
echo   DONE
echo.
echo   installer_out\LedgerSetup-5.8.exe
echo.
echo   THAT is the file you give people. Not dist\Ledger.exe.
echo   Send it to anyone - they double-click it, click Next a
echo   few times, and Ledger is installed with a shortcut.
echo ============================================================
echo.
if exist "installer_out" explorer "installer_out"
pause
