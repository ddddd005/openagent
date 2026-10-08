@echo off
setlocal
title OpenAgent local worktree
rem Keep this entry point ASCII so paths with spaces and Unicode remain intact.
if not exist "%~dp0scripts\start-services.ps1" (
    echo [ERROR] Missing scripts\start-services.ps1 in this OpenAgent checkout.
    if not defined NO_PAUSE pause
    exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-services.ps1" %*
set "START_EXIT=%ERRORLEVEL%"
if not defined NO_PAUSE pause
exit /b %START_EXIT%
