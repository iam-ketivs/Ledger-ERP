@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Use this when dist\Ledger.exe already exists and you only want the installer -
REM it skips the five-minute rebuild.

echo ============================================================
echo   Making the installer from the Ledger.exe you already built
echo ============================================================
echo.

if not exist "dist\Ledger.exe" (
    echo   dist\Ledger.exe is not there. Run build_exe.bat first,
    echo   or use "BUILD INSTALLER.bat" which does both.
    pause & exit /b 1
)

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
    echo   Cannot find ISCC.exe. Search your PC for it and tell me the path.
    pause & exit /b 1
)
set ISCC=!ISCC:"=!
echo Using: !ISCC!
echo.

"!ISCC!" installer.iss
if errorlevel 1 ( echo. & echo   Failed - see the message above. & pause & exit /b 1 )

echo.
echo ============================================================
echo   DONE:  installer_out\LedgerSetup-5.8.exe
echo   That is the file you give people.
echo ============================================================
echo.
if exist "installer_out" explorer "installer_out"
pause
