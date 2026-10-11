@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_python.bat"
if errorlevel 1 ( pause & exit /b 1 )
set "PW=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PW%" set "PW=%PYEXE%"
start "" "%PW%" chaoxing_scanner.py desktop
exit
