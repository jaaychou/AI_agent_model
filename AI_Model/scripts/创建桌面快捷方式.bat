@echo off
chcp 65001 >nul
title 创建本地Agent桌面快捷方式
echo 正在创建桌面快捷方式...

powershell -NoProfile -Command ^
  "$Base = Split-Path -Parent '%~dp0'; ^
   $Python = Join-Path $Base 'venv-webui\Scripts\python.exe'; ^
   if (!(Test-Path $Python)) { $Python = Join-Path $Base 'venv-comfy\Scripts\python.exe' }; ^
   & $Python (Join-Path $Base 'agent\make_shortcut.py')"

echo.
pause
