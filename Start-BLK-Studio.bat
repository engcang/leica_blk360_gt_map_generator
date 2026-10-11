@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
if errorlevel 1 (
    echo.
    echo Setup failed. See the error above and build\setup.log if available.
    pause
    exit /b 1
)
