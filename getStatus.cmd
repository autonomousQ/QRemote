@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0getStatus.ps1"
timeout /t 5 /nobreak >nul