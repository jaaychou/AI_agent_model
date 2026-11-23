@echo off
chcp 65001 >nul
title 本地 Agent - Claude Code 风格助手
echo ============================================
echo   本地 AI Agent 启动中
echo   模型: qwen2.5-coder:7b（本地，不花积分）
echo   工作空间: D:\Program\AI_Model_agent
echo ============================================
echo.
echo   提示：先确保 Ollama 已启动（脚本1）
echo   输入 /help 查看命令，/quit 退出
echo.
cd /d "D:\Program\AI_Model_agent\AI_Model\agent"
"D:\Program\AI_Model_agent\AI_Model\venv-comfy\Scripts\python.exe" agent_cli.py
pause
