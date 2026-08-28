@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title SpotiDown GitHub Publisher by mebularts
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Publish-GitHub.ps1"
echo.
if errorlevel 1 (
  echo [ERROR] Publish failed. See the message above.
) else (
  echo [OK] Publish completed.
)
pause
endlocal
