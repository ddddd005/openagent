@echo off
setlocal
title OpenAgent develop
rem This explicit entry never selects the stable main checkout.
if not exist "%~dp0scripts\start-branch-services.ps1" (
    echo [ERROR] Missing scripts\start-branch-services.ps1 in this OpenAgent checkout.
    if not defined NO_PAUSE pause
    exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-branch-services.ps1" -Branch develop %*
set "START_EXIT=%ERRORLEVEL%"
if not defined NO_PAUSE pause
exit /b %START_EXIT%
