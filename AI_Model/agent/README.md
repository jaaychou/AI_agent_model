# 本地 AI Agent（子项目）

自研桌面 Agent，模型用同目录上层的 4 个本地 Ollama 模型。入口：桌面「本地AI Agent」图标 → `agent_gui.py`。

## 入口与运行

- `agent_gui.py`：Codex 风格桌面 GUI（主入口，`5-启动本地Agent.bat` 拉起）
- `agent_cli.py`：命令行版（无 GUI，高危能力默认关，用环境变量 `AGENT_ENABLE_*=1` 开启）
- 依赖：`venv-comfy`（Pillow/keyring 等）；测试一律用 `AI_Model/venv-comfy/Scripts/python.exe`

## 目录结构

```
agent/
├─ agent_gui.py / agent_cli.py   入口
├─ config.json / config.example.json   配置（前者被 gitignore）
├─ mcp_servers.json / .example.json    MCP 连接器配置
├─ api_keys.example.json   外部 API key 模板（真实 api_keys.json 被忽略）
├─ core/   所有能力模块（见下）
├─ skills/  技能（SKILL.md + frontmatter）
├─ tools/   本地 MCP 服务脚本（email_mcp_server.py 等）
├─ tests/   unittest（全套 182+ 项）
└─ data/    会话库、RAG 索引、图表/动画/抓包输出（被忽略）
```

## core/ 模块一览（按用途）

| 类别 | 模块 | 主要工具 |
|---|---|---|
| 基座 | `llm.py` / `tools.py` / `agent.py` | provider、工具注册表、ReAct 循环 |
| 记忆/知识 | `sessions.py` / `rag.py` | SQLite 会话、FTS5 知识库 |
| 扩展 | `skills.py` / `mcp.py` / `meta_tools.py` | 技能、MCP、工具自发现/安装/自造 |
| 系统 | `system_control.py` / `privileged_tools.py` | 进程服务窗口串口、固件/启动项/Win+R（授权门控） |
| 网络 | `web_search.py` / `web_tools.py` / `weather_tools.py` / `capture_tools.py` / `network_observe.py` | 搜索/抓取/天气/抓包/只读观测 |
| 数据 | `data_tools.py` / `dsp_tools.py` / `mermaid_tools.py` | 图表分析、信号处理、流程图渲染 |
| 自治 | `autonomy.py` / `autopilot.py` | L4 规划验收、L5 围栏自治 |
| 多模型 | `model_router.py` | 列模型能力、跨模型委托 |
| 动画 | `manim_tools.py` | 3b1b ManimGL 渲染（引擎在 `../tools/manim`） |
| 其他 | `external_apis.py` / `sandbox.py` / `git_manager.py` / `progress.py` | 地图车票、隔离沙盒、Git、进度 |

## 设计铁律（维护时勿破坏）

1. **自主执行**：模型必须自己调用工具干活，禁止只给建议；通用兜底工具常驻可见（见 `agent.py` 的 `ALWAYS_AVAILABLE`）。
2. **授权门控**：高危能力（固件/启动项/抓包）默认关闭，GUI 开关开启才注册，写操作逐次 confirm。
3. **诚实降级**：缺 key/凭据、硬件不支持、能力不足时如实说明，绝不编造结果或伪造抓包/渲染。
4. **工具筛选省 token**：按意图给 schema + 兜底集；不要一次塞全部工具给小模型。

## 测试

```bash
cd D:/Program/AI_Model_agent
AI_Model/venv-comfy/Scripts/python.exe -m unittest discover -s AI_Model/agent/tests
```

当前全套 182+ 项通过。改代码后需完全退出并重启 Agent 才生效。
