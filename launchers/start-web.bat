@echo off
title BioData Agent Web

REM ===================================================================
REM  Locate the project root (the folder that contains scripts\run_web.py)
REM  so this launcher works from inside the project, from the submission
REM  package root, or when copied to a common location on this PC.
REM ===================================================================
set "ROOT="

REM 1) same folder as this launcher (normal: the launcher sits in the project)
if exist "%~dp0scripts\run_web.py" set "ROOT=%~dp0"

REM 2) submission-package layout: the project sits in a subfolder next to this
REM    launcher, e.g. <package>\biodata-agent\. Check the conventional name first.
if not defined ROOT if exist "%~dp0biodata-agent\scripts\run_web.py" set "ROOT=%~dp0biodata-agent\"

REM 3) any other immediate subfolder that contains the project (folder renamed,
REM    or unpacked with a different name). First match found by the shell wins.
if not defined ROOT for /d %%D in ("%~dp0*") do if exist "%%~fD\scripts\run_web.py" set "ROOT=%%~fD\"

REM 4) tolerate one extra nesting level from "Extract All" inside the package
if not defined ROOT for /d %%D in ("%~dp0*") do if exist "%%~fD\biodata-agent\scripts\run_web.py" set "ROOT=%%~fD\biodata-agent\"

REM 4.5) repo clone layout: this launcher sits in launchers\ under the project
REM    root (2026-08-27 top-level cleanup) - the project root is one level up.
if not defined ROOT if exist "%~dp0..\scripts\run_web.py" set "ROOT=%~dp0..\"

REM 5) common install locations (used when the launcher is copied out)
if not defined ROOT if exist "%USERPROFILE%\Desktop\biodata-agent\scripts\run_web.py" set "ROOT=%USERPROFILE%\Desktop\biodata-agent\"
if not defined ROOT if exist "%USERPROFILE%\OneDrive\Desktop\biodata-agent\scripts\run_web.py" set "ROOT=%USERPROFILE%\OneDrive\Desktop\biodata-agent\"
if not defined ROOT if exist "%USERPROFILE%\Downloads\biodata-agent\scripts\run_web.py" set "ROOT=%USERPROFILE%\Downloads\biodata-agent\"
if not defined ROOT if exist "%USERPROFILE%\Desktop\biodata-agent\biodata-agent\scripts\run_web.py" set "ROOT=%USERPROFILE%\Desktop\biodata-agent\biodata-agent\"
if not defined ROOT if exist "%USERPROFILE%\OneDrive\Desktop\biodata-agent\biodata-agent\scripts\run_web.py" set "ROOT=%USERPROFILE%\OneDrive\Desktop\biodata-agent\biodata-agent\"

if not defined ROOT (
  echo [!] Project files not found ^(scripts\run_web.py^).
  echo     Keep this launcher next to the biodata-agent folder,
  echo     or inside it.
  echo.
  pause
  exit /b 1
)

cd /d "%ROOT%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\launch_web.ps1" -ProjectPath "%ROOT%."
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo.
  echo [!] Startup failed. See the message above.
  pause
)
exit /b %EXITCODE%
