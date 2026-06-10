@echo off
chcp 65001 >nul 2>&1
REM === Agent SDK Onboarder — Environment Diagnostics ===
REM Runs prerequisite checks and prints a colored report.

cd /d "%~dp0"
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File ".\Diagnose-Environment.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\Diagnose-Environment.ps1"
)

echo.
pause
