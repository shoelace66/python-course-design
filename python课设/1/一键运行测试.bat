@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title MusicScope 自动化测试

echo 正在执行 MusicScope 自动化测试……
python -W error::ResourceWarning -m unittest discover -s tests -v
echo.
if errorlevel 1 (
    echo [结果] 有测试未通过。
) else (
    echo [结果] 全部测试通过。
)
pause
endlocal

