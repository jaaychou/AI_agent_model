@echo off
chcp 65001 >nul
title AI 资源守护 - 防止卡死/显卡超时
echo ============================================
echo   AI 资源守护程序
echo   监控: 内存 / CPU / 显存 / GPU温度
echo   超限时自动限流 -^> 中断生成 -^> 紧急保护
echo   保持此窗口开启，关闭即停止守护
echo ============================================
echo.
cd /d "D:\Program\AI_Model_agent\AI_Model\guard"
"D:\Program\AI_Model_agent\AI_Model\venv-comfy\Scripts\python.exe" resource_guard.py
pause
