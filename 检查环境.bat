@echo off
chcp 65001 >nul
title 检查环境
cd /d "%~dp0"
call "%~dp0_python.bat"
if errorlevel 1 ( pause & exit /b 1 )
"%PYEXE%" chaoxing_scanner.py doctor
echo.
pause
