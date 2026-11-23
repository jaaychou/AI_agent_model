@echo off
chcp 65001 >nul
title ComfyUI - 本地生图
echo ============================================
echo   ComfyUI 启动中（Z-Image-Turbo 文生图）
echo   启动后浏览器打开: http://localhost:8188
echo   保持此窗口开启，关闭即停止服务
echo ============================================
echo.
set HF_ENDPOINT=https://hf-mirror.com
cd /d "D:\Program\AI_Model_agent\AI_Model\ComfyUI"
"D:\Program\AI_Model_agent\AI_Model\venv-comfy\Scripts\python.exe" main.py --port 8188 --lowvram --cpu-vae --reserve-vram 0.5
pause
