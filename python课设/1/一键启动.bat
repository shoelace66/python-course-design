@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title MusicScope 音乐特征分析器

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python。
    echo 请安装 Python 3.10 或更高版本，并勾选“Add Python to PATH”。
    pause
    exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo [错误] MusicScope 需要 Python 3.10 或更高版本。
    python --version
    pause
    exit /b 1
)

echo 正在启动 MusicScope……
echo 浏览器会自动打开；关闭本窗口即可停止服务。
python app.py

if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，请将上方错误信息截图用于排查。
    pause
)
endlocal

