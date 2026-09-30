@echo off
rem ============================================================
rem  一键构建 PSBatteryTray 单文件 exe
rem  产物：Release\PSBatteryTray.exe
rem ============================================================
setlocal
cd /d "%~dp0\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "build\build.ps1" %*
if errorlevel 1 (
  echo.
  echo 构建失败，请查看上面的输出。
  pause
  exit /b 1
)
echo.
pause
