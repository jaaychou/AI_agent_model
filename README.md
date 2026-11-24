# AI_agent_model

本地 AI Agent 与本地模型部署工程（离线优先，不依赖云端积分）。

> 最近复核：2026-11-24 13:10（GMT+8）

## 快速开始

**双击桌面「本地AI Agent」图标**即可。Ollama 会自动拉起，无需手动开服务或浏览器。

## 读文档的顺序（换 AI 接手请照此顺序）

1. `AI_Model/交接文档.md` — 全貌、精确到秒的时间线、技术坑、Git 交接、后续 roadmap
2. `AI_Model/agent/README.md` — Agent 子项目结构与工具一览
3. `AI_Model/agent/使用说明.md` — GUI 全部功能与操作
4. `AI_Model/使用说明.md` — 模型 / 生图 / 资源守护底座
5. `AI_Model/docs/` — 专题文档（能力等级、能力清单、新模型集成与权限边界）
6. `.workbuddy/memory/` — 逐轮排错细节（按日期）

## 内容概览

- 4 个本地 Ollama 模型（qwen3.5:4b / deepseek-r1:8b / qwen2.5-coder:7b / qwen2.5vl:7b）
- 自研桌面 Agent：ReAct 循环、7+ 内置工具、Skill、MCP、会话记忆、本地 RAG、Git 面板、进度 ETA、Playwright 浏览器自动化、文生图集成
- 内置 Bing RSS 实时搜索、本机日期时间查询，以及 PDF/DOCX 提取为 Markdown
- Ollama 流式工具参数校验；参数 JSON 格式错误时停止执行并要求模型重试
- 本地 IMAP/SMTP 邮箱 MCP 起步实现（Gmail/QQ；需用户配置应用密码和邮箱服务器，发送逐次确认）
- GUI 支持对话复制、模型相关的文件类型提示、资源管理器文件复制粘贴/拖放，以及生成图片放大和复制
- ComfyUI + Z-Image-Turbo 本地文生图
- 资源守护程序（防显存/内存爆满导致蓝屏）
- Open WebUI + 联网搜索/代码执行工具
- **围栏式 L5 自治**（autopilot）+ 底层能力：磁盘扫描、WiFi/蓝牙只读观测、实时网速、DSP 频谱/滤波
- **高级权限开关**（默认关，逐项授权）：固件/BIOS 只读、开机自启动、Win+R 命令、网络抓包（Npcap+管理员）
- **数据能力**：matplotlib 画图弹窗预览、pandas 分析、隔离 venv
- **3b1b Manim 动画**：ManimGL 原版 + 线代本质 16 章源码，已实测渲染 mp4（`scripts/恢复Manim动画环境.bat` 一键恢复）
- **多模型协同**：A 模型可委托子任务给更擅长的 B 模型（实测成功）；**Mermaid 流程图**实时渲染
- **运行模式切换**：普通 / L3 计划 / L4 回环；联网搜索 On/Off 开关

## 运行环境（不进仓库，需另备）

`AI_Model/` 下的 `models/`(18GB)、`ComfyUI/`(12GB)、`Ollama/`、`venv-comfy/`、`venv-webui/`、`downloads/` 及 `agent/config.json`、`agent/mcp_servers.json`、`agent/data/` 均被 `.gitignore` 排除。
已提供 `agent/config.example.json` 与 `agent/mcp_servers.example.json` 模板，复制改名即可恢复配置。

## Git

- 远端：`https://github.com/jaaychou/AI_agent_model.git`（注意是 `jaaychou`）
- 分支：`main`
- Agent 内 Git 新手操作：打开 `☰` → `Git`，按 `AI_Model/agent/使用说明.md` 的“Git 是什么，第一次怎么连接”逐步操作。当前已配置 `origin`，本地与远端 `main` 已同步（最新提交 `c11dda8`，本轮改动提交后会更新）。
- `src refspec HEAD does not match any` 表示本机没有提交；先用 Git 页的“暂存并提交”，再推送。
