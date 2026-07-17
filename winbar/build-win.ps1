# 打包 ai-limit Windows 托盘版为单文件 exe。
# 在仓库根目录的 PowerShell 里运行：  powershell -ExecutionPolicy Bypass -File winbar\build-win.ps1
# 需要：Windows + Python 3.10+（官方安装包，自带 tkinter）
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # 仓库根

if (-not (Test-Path ".venv-win")) {
    python -m venv .venv-win
}
& .venv-win\Scripts\pip.exe install -q -r winbar\requirements-win.txt pyinstaller

# --onefile 单 exe；--noconsole 无黑窗；usage.py 以数据文件塞进去由 sys.path 加载
& .venv-win\Scripts\pyinstaller.exe --noconfirm --onefile --noconsole `
    --name ai-limit-tray `
    --distpath winbar\dist `
    --workpath winbar\build `
    --specpath winbar\build `
    --add-data "$PWD\usage.py;." `
    --hidden-import pystray._win32 `
    winbar\ai-limit-tray.py

Write-Host "OK -> winbar\dist\ai-limit-tray.exe"
