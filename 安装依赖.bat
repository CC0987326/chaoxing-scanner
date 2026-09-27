@echo off
chcp 65001 >nul
title 安装 / 修复依赖
cd /d "%~dp0"

set "PYEXE="
if exist "%~dp0runtime\venv\Scripts\python.exe" set "PYEXE=%~dp0runtime\venv\Scripts\python.exe"
if not defined PYEXE if exist "C:\Users\32232\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PYEXE=C:\Users\32232\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if defined PYEXE (
    "%PYEXE%" -c "import playwright, ddddocr, PIL" >nul 2>nul
    if not errorlevel 1 (
        echo [OK] 依赖已经齐全，不用装。
        echo      Python 环境：%PYEXE%
        echo.
        pause
        exit /b 0
    )
    echo 正在向现有环境补充依赖，请稍候 ...
    "%PYEXE%" -m pip install -r "%~dp0requirements.txt"
    goto done
)

echo 没有找到可用环境，正在创建项目独立虚拟环境 runtime\venv ...
set "BASE=python"
if exist "C:\Users\32232\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "BASE=C:\Users\32232\.workbuddy\binaries\python\versions\3.13.12\python.exe"
"%BASE%" -m venv "%~dp0runtime\venv"
if errorlevel 1 (
    echo.
    echo [错误] 创建虚拟环境失败，请先安装 Python 3.10 以上版本并勾选 Add to PATH。
    pause
    exit /b 1
)
"%~dp0runtime\venv\Scripts\python.exe" -m pip install -U pip
"%~dp0runtime\venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"

:done
echo.
echo 处理完毕。可以运行 "检查环境.bat" 验证。
echo.
pause
