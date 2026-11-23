# AI_agent_model

本地 AI Agent 与本地模型部署工程（离线优先，不依赖云端积分）。

> 最近整理：2026-11-23 19:26:45（GMT+8）

## 快速开始

**双击桌面「本地AI Agent」图标**即可。Ollama 会自动拉起，无需手动开服务或浏览器。

## 读文档的顺序（换 AI 接手请照此顺序）

1. `AI_Model/交接文档.md` — 全貌、精确到秒的时间线、10 个技术坑、Git 交接、后续 roadmap
2. `AI_Model/agent/使用说明.md` — GUI 全部功能与操作
3. `AI_Model/使用说明.md` — 模型 / 生图 / 资源守护底座
4. `.workbuddy/memory/2026-11-23.md` — 逐轮排错细节

## 内容概览

- 4 个本地 Ollama 模型（qwen3.5:4b / deepseek-r1:8b / qwen2.5-coder:7b / qwen2.5vl:7b）
- 自研桌面 Agent：ReAct 循环、7+ 内置工具、Skill、MCP、会话记忆、本地 RAG、Git 面板、进度 ETA、Playwright 浏览器自动化、文生图集成
- ComfyUI + Z-Image-Turbo 本地文生图
- 资源守护程序（防显存/内存爆满导致蓝屏）
- Open WebUI + 联网搜索/代码执行工具

## 运行环境（不进仓库，需另备）

`AI_Model/` 下的 `models/`(18GB)、`ComfyUI/`(12GB)、`Ollama/`、`venv-comfy/`、`venv-webui/`、`downloads/` 及 `agent/config.json`、`agent/mcp_servers.json`、`agent/data/` 均被 `.gitignore` 排除。
已提供 `agent/config.example.json` 与 `agent/mcp_servers.example.json` 模板，复制改名即可恢复配置。

## Git

- 远端：`https://github.com/jaaychou/AI_agent_model.git`（注意是 `jaaychou`）
- 分支：`main`
- 日常：`git add -A && git commit -m "说明" && git push origin main`
