@echo off
rem GridBot -- paper spot-grid bot on the GridPick Oracle v3.1 scanner.
rem One window. The scanner runs hidden inside it and dies with it.
rem A second click while it is running just says so; it never starts a twin.
setlocal
cd /d "%~dp0"
title GridBot (paper)
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

:loop
python -X utf8 gridbot.py %*
set rc=%errorlevel%
if "%rc%"=="0" goto done
if "%rc%"=="3" (
  echo GridBot is already running from this folder.
  timeout /t 8 >nul
  goto done
)
if "%rc%"=="2" (
  echo GridBot could not start: see the message above and gridbot.log
  pause
  goto done
)
echo GridBot exited with code %rc% -- restarting in 15s. Close this window to stop it.
timeout /t 15 >nul
goto loop

:done
endlocal
