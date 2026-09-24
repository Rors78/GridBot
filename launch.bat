@echo off
:: ===================================================================
:: GridPick -- the desktop shortcut. KILL, THEN LAUNCH. ONE window.
::
:: Every click:
::   1. stops everything GridBot: the bot's run.bat loop, gridbot.py, its
::      oracle.py scanner, and any open Lattice console (gridbot_stop.ps1);
::   2. starts run.bat again with NO window (restart loop and all). Open
::      grids are restored from state.json and catch up from REST;
::   3. turns THIS window into the GRIDPICK LATTICE console (tui\lattice.py).
::
:: The Lattice is read-only: it reads status.json, scan.json, gridbot.log.
:: Quitting it (q) leaves the bot trading; click the shortcut to get it back
:: (that click restarts the bot too -- it is a kill launcher by design).
::
::   launch.bat stop     stop everything, start nothing
::
:: ASCII ONLY in this file: cmd.exe misparses comment lines holding
:: non-ASCII characters.
:: ===================================================================
setlocal
cd /d "%~dp0"
title GridPick

echo.
echo   GridPick: stopping any running GridBot first
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gridbot_stop.ps1"
if /i "%~1"=="stop" (
  %SystemRoot%\System32\timeout.exe /t 3 >nul
  exit /b 0
)

:: Let the killed processes die and the OS release gridbot.lock.
%SystemRoot%\System32\timeout.exe /t 3 >nul

echo.
echo   GridPick: starting GridBot with no window
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0run.bat' -WorkingDirectory '%~dp0' -WindowStyle Hidden"

:: Give the bot a moment to write a fresh status.json, so the first frame
:: is data. Not a dependency: the console polls every second.
%SystemRoot%\System32\timeout.exe /t 4 >nul

title GridPick Lattice
mode con cols=140 lines=45 >nul 2>&1
set PYTHONUTF8=1

:loop
python -X utf8 "%~dp0tui\lattice.py"
set rc=%errorlevel%
:: 0 = q pressed. Anything else is a crash worth retrying.
if not "%rc%"=="0" (
  echo.
  echo   GridPick Lattice exited with code %rc%. Restarting in 10s -- close this window to stop.
  %SystemRoot%\System32\timeout.exe /t 10 >nul
  goto loop
)
endlocal
