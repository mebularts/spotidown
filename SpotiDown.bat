@echo off
setlocal
cd /d "%~dp0"
title SpotiDown by mebularts
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 spotidown.py menu
) else (
  python spotidown.py menu
)
endlocal
