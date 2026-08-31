$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "没有找到 Python。请安装 Python 3.10 或更高版本，并加入 PATH。"
}

Write-Host "正在启动 MusicScope，浏览器会自动打开……" -ForegroundColor Cyan
python app.py

