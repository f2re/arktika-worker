@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (echo Сначала выполните install.cmd. & pause & exit /b 2)
".venv\Scripts\python.exe" server.py %*
if errorlevel 1 pause
