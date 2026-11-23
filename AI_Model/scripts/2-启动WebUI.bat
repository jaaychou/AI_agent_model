@echo off
chcp 65001 >nul
title Open WebUI - 网页聊天界面
echo ============================================
echo   Open WebUI 启动中...
echo   启动后浏览器打开: http://localhost:8080
echo   首次加载较慢，请耐心等待
echo   保持此窗口开启，关闭即停止服务
echo ============================================
echo.
set HF_ENDPOINT=https://hf-mirror.com
set DATA_DIR=D:\Program\AI_Model_agent\AI_Model\webui_data
set OLLAMA_BASE_URL=http://127.0.0.1:11434
"D:\Program\AI_Model_agent\AI_Model\venv-webui\Scripts\open-webui.exe" serve --host 127.0.0.1 --port 8080
pause
