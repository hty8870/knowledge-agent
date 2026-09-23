@echo off
title BioData Agent - Create Desktop Shortcut
setlocal

REM ===================================================================
REM  Create a desktop shortcut to the BioData Agent launcher.
REM  - The launcher used is start-web.bat inside the biodata-agent
REM    folder. It is the same program as the Chinese-named launcher
REM    in the package root (the manual states they are the same thing);
REM    keeping this file pure ASCII avoids codepage issues in cmd.
REM  - The desktop path comes from [Environment]::GetFolderPath('Desktop'),
REM    so it still works when OneDrive redirects the desktop folder.
REM  - Running this file again simply overwrites the existing shortcut,
REM    so it is safe to repeat (idempotent).
REM ===================================================================

set "PKG=%~dp0"
set "TARGET=%PKG%biodata-agent\start-web.bat"

REM  Repo clone layout (2026-08-27 top-level cleanup): this file and
REM  start-web.bat both live in launchers\ under the project root.
if not exist "%TARGET%" if exist "%PKG%start-web.bat" set "TARGET=%PKG%start-web.bat"

if not exist "%TARGET%" (
  echo [!] Launcher not found: %TARGET%
  echo     Keep this file next to the biodata-agent folder, then run it again.
  echo.
  pause
  exit /b 1
)

REM  Use WScript.Shell via PowerShell: TargetPath and WorkingDirectory are
REM  set from the absolute path of this folder, so the shortcut always
REM  points at this copy of the app instead of a guessed install location.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $desktop = [Environment]::GetFolderPath('Desktop'); $lnk = $ws.CreateShortcut((Join-Path $desktop 'BioData Agent.lnk')); $lnk.TargetPath = '%TARGET%'; $lnk.WorkingDirectory = '%PKG%'; $lnk.Description = 'Open the BioData Agent web frontend'; $lnk.Save(); Write-Host 'Shortcut created on the desktop: BioData Agent.lnk'"
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo.
  echo [!] Failed to create the desktop shortcut. See the message above.
  echo.
  pause
  exit /b %EXITCODE%
)

echo [i] Done. You can now start BioData Agent from the desktop shortcut.
echo     Note: the shortcut points at this folder, so keep it in place;
echo     if you move the folder, run this file again to refresh the shortcut.
echo.
pause
exit /b 0
