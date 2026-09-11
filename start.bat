@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title grok-rego
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
    echo.
    echo 启动失败，退出码 %ERR%。
    pause
)
exit /b %ERR%
