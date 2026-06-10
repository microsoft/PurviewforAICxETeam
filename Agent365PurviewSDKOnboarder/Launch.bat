@echo off
REM Agent SDK Onboarder — one-click launcher
REM Double-click this file to open the onboarding UI in your browser.

setlocal
cd /d "%~dp0"

REM Prefer PowerShell 7 (pwsh) if available, otherwise fall back to Windows PowerShell.
where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-AgentOnboarder.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-AgentOnboarder.ps1" %*
)

REM Keep the window open so the user can read any error before it closes.
echo.
echo (Press any key to close this window.)
pause >nul
endlocal
