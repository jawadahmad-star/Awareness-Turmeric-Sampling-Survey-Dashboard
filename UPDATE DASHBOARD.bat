@echo off
REM ===================================================================
REM  TURMERIC QUALITY DASHBOARD - daily update
REM
REM  1. Put the new SurveyCTO exports here:
REM       data_in\awareness\   (awareness survey CSV)
REM       data_in\turmeric\    (sampling CSV + the two repeat-group CSVs)
REM  2. Double-click this file.
REM
REM  It rebuilds the encrypted dashboard payload, commits it, and pushes
REM  to GitHub. The live site updates about a minute after that.
REM ===================================================================

cd /d "%~dp0"
title Turmeric Quality Dashboard - Daily Update

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found on this machine.
  echo   Install Python 3 from https://python.org and tick "Add to PATH".
  echo.
  pause
  exit /b 1
)

python scripts\daily_update.py %*
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
  echo   Finished successfully.
) else (
  echo   Finished with errors - see the messages above and logs\ for detail.
)
echo.
pause
exit /b %RC%
