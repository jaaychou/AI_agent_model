@echo off
chcp 65001 >nul
title Ollama 服务 - AI 模型后端
echo ============================================
echo   启动 Ollama 模型服务（RTX 4060 Vulkan 加速）
echo   服务地址: http://127.0.0.1:11434
echo   保持此窗口开启，关闭即停止服务
echo ============================================
echo.
set OLLAMA_MODELS=D:\Program\AI_Model_agent\AI_Model\models
set OLLAMA_LLM_LIBRARY=vulkan
"D:\Program\AI_Model_agent\AI_Model\Ollama\ollama.exe" serve
pause
