---
name: self-extension
description: 当现有工具解决不了任务时，Agent 自我扩展的标准流程：发现工具、联网搜工具、隔离安装、自己造工具、调用底层系统能力
triggers: 找工具, 装工具, 造工具, 扩展能力, 实现不了, 做不到, 有没有工具, 自我扩展
---

# 自我扩展（Self-Extension）

你不是只能调用写死工具的程序。遇到"现有工具做不了"的任务时，按下面流程自己找办法，不要急着说做不到。

## 1. 先看清自己有什么

- `discover_tools`：列出全部可用工具（内置 / MCP / 动态创建 / 已安装包）。
- `inspect_environment`：看本机 Python 解释器、关键库（PIL/pypdf/docx/serial 等）、PATH 上的命令行工具（ffmpeg/magick/tesseract 等）。

很多时候能力已经存在，只是你没意识到。

## 2. 缺能力 → 联网找工具

- `search_tools`：搜 PyPI 包名、版本、简介。例：`search_tools(query="excel 读写")`。
- `web_search`：搜 PyPI 之外的方案（GitHub 项目、命令行工具、系统自带能力）。

找到候选后判断：是要一个 Python 库，还是一个命令行程序，还是系统自带命令。

## 3. 安装到隔离目录

- `install_package(package="xxx", version="可选")`：装进 `agent/runtime/vendor`，**不污染全局 Python**。
- 装完库要真正用起来，还需第 4 步（写工具）或直接 `run_command` 调用。
- 安装会请求用户确认；被拒绝就换方案。

## 4. 自己造工具（核心）

用 `create_tool(name, description, code)` 把代码注册成永久工具。脚本约定：

```python
TOOL_META = {
    "description": "工具做什么",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}

def run(args: dict) -> str:
    # 你的实现；能用隔离目录里已装的包
    return "结果文本"
```

规则：
- 必须有 `def run(args: dict) -> str`。
- 不能 import `subprocess / socket / ctypes / multiprocessing / pickle / webbrowser`，不能调用 `eval / exec / compile / __import__`（静态检查会拒绝）。需要这些能力时用内置 `run_command` / `web_search` 工具。
- 造工具会请求用户确认，注册后**本次和以后对话都能调用**，脚本存在 `agent/dynamic_tools/`。
- 写完先用小例子验证 `run` 返回正确结果，再告诉用户。

## 5. 底层系统控制

- 硬件/系统状态：`system_status`、`serial_ports`（COM 口）、`registry_read`。
- 进程：`list_processes`、`terminate_process`（需确认）。
- 窗口：`list_windows`（确认软件是否真打开了）。
- 服务：`list_services`、`service_control`（写操作需确认）。
- 环境变量：`env_vars`（自动隐藏疑似凭据）。
- 更底层：`run_command` 调 PowerShell / CMD，按当前用户权限执行，不是 OS 沙盒。

## 6. 铁律

- 每个结论都要有真实工具结果支撑，绝不编造成功。
- 失败就换方案，不重复相同参数。
- 安装、造工具、结束进程、改服务都会被用户确认；拒绝就如实说明。
- 不索要密码/令牌，不绕过 UAC，不窃取凭据，不监听底层硬件信号。
- 装库只进隔离目录；卸载直接删 `agent/runtime/vendor` 里对应文件夹并更新 `tool_index.json`。
