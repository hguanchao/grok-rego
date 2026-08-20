@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title grok-rego
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
exit /b %ERRORLEVEL%
