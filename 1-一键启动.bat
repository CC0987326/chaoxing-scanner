@echo off
chcp 65001 >nul
title 学习通巡检工具
cd /d "%~dp0"
call "%~dp0_python.bat"
if errorlevel 1 ( pause & exit /b 1 )

echo.
echo   正在启动学习通巡检工具的控制台界面 ...
echo   浏览器会自动打开操作页面。
echo   用完退出：网页里点「退出程序」，或直接关掉本窗口。
echo.
"%PYEXE%" chaoxing_scanner.py gui
if errorlevel 1 (
    echo.
    echo [程序异常退出] 把上面的报错内容截图发给助手即可。
    pause
)
