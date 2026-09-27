@echo off
chcp 65001 >nul
title 学习通 · 登录
cd /d "%~dp0"
call "%~dp0_python.bat"
if errorlevel 1 ( pause & exit /b 1 )

echo.
echo   即将打开浏览器窗口，请在窗口里完成登录：
echo     · 推荐用手机「学习通」APP 扫右侧二维码
echo     · 也可以输手机号 + 密码，或切到「验证码登录」
echo   登录成功后窗口会自动关闭，会话会保存下来，之后扫描就不用再登了。
echo.
"%PYEXE%" chaoxing_scanner.py login
echo.
pause
