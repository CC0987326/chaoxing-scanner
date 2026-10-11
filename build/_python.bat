@echo off
rem ===================================================================
rem  解析一个可用的 Python 解释器，结果放进变量 PYEXE，供其它 bat 调用
rem  优先级：项目独立环境 > 已装好依赖的环境 > 系统 PATH 里的 python
rem ===================================================================
set "PYEXE="
set "VENV1=%~dp0runtime\venv\Scripts\python.exe"
set "VENV2=C:\Users\32232\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if exist "%VENV1%" set "PYEXE=%VENV1%"
if not defined PYEXE if exist "%VENV2%" set "PYEXE=%VENV2%"
if not defined PYEXE (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%i"
    )
)
if not defined PYEXE (
    echo [错误] 找不到 Python。请先安装 Python 3.10 以上版本，安装时勾选 "Add to PATH"。
    echo        然后运行 "安装依赖.bat"。
    exit /b 1
)
exit /b 0
