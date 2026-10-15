@echo off
setlocal enabledelayedexpansion
echo ============================================================
echo   Looking for ISCC.exe (the Inno Setup compiler)
echo ============================================================
echo.
echo Searching %SystemDrive% - this takes a minute...
echo.
set FOUND=0
for /f "delims=" %%I in ('dir /s /b "%SystemDrive%\ISCC.exe" 2^>nul') do (
  echo   FOUND: %%I
  set FOUND=1
)
if "!FOUND!"=="0" (
  echo   ISCC.exe is not on %SystemDrive%.
  echo.
  echo   Either Inno Setup is on another drive, or the install did
  echo   not finish. Re-install it from https://jrsoftware.org/isdl.php
  echo   and take the standard "Inno Setup" download, not the
  echo   "Inno Script Studio" or QuickStart Pack.
) else (
  echo.
  echo   Copy the path above and paste it when BUILD INSTALLER asks.
)
echo.
pause
