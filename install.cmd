@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
where py >nul 2>nul
if errorlevel 1 (python install.py %*) else (py -3 install.py %*)
if errorlevel 1 (echo Установка не завершена. Требуется Python 3.10 или новее. & pause & exit /b 1)
pause
