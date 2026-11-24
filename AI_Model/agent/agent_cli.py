#!/usr/bin/env python3
"""本地 Agent —— Claude Code / Codex 风格的命令行 AI 助手。

用法：
  python agent_cli.py                     # 进入交互模式
  python agent_cli.py -w D:/myproject     # 指定工作空间
  python agent_cli.py -m qwen3.5:4b       # 指定模型
  python agent_cli.py -p "帮我写个函数"   # 单次提问后退出
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core.llm import OllamaProvider, OpenAICompatibleProvider
from core.tools import ToolRegistry, register_builtin_tools
from core.agent import Agent, AgentConfig
from core.skills import load_skills, build_skills_prompt, register_skill_tools
from core.mcp import load_mcp_servers, register_mcp_tools

CONFIG_PATH = BASE / "config.json"
MCP_PATH = BASE / "mcp_servers.json"
SKILLS_DIR = BASE / "skills"


def load_config() -> dict:
    defaults = {
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5-coder:7b",
        "workspace": ".",
        "max_steps": 30,
        "max_context_messages": 16,
        "system_prompt": "",
    }
    if CONFIG_PATH.exists():
        try:
            defaults.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[警告] 配置文件读取失败，用默认值: {e}")
    return defaults


def build_system_prompt(registry: ToolRegistry, skills_prompt: str = "") -> str:
    # 注意：不要在 system prompt 里堆工具描述或 JSON 示例，
    # Ollama 的模板渲染会把花括号当语法解析导致 400。工具从 tools schema 自动可见。
    return (
        "你是一个运行在用户本地电脑上的 AI 编程助手，类似 Claude Code。\n"
        "你有工具可以读写文件、搜索代码、执行终端命令。\n"
        "规则：需要操作时直接调用工具，不要只描述。修改文件前先读取现有内容。"
        "看完工具结果后继续下一步，直到任务完成。最后给出简洁总结。\n"
        f"{skills_prompt}"
    )


def make_agent(args, cfg: dict) -> Agent:
    # 检查 Ollama
    provider = OllamaProvider(base_url=cfg["base_url"], default_model=cfg["model"])
    if not provider.check():
        print("[错误] 连不上 Ollama，请先双击 scripts\\1-启动Ollama.bat")
        sys.exit(1)

    # 工作空间
    workspace = args.workspace or cfg.get("workspace", ".")
    workspace = str(Path(workspace).resolve())
    if not Path(workspace).is_dir():
        print(f"[错误] 工作空间不存在：{workspace}")
        sys.exit(1)

    def confirm(msg):
        try:
            return input(msg).strip().lower() in ("y", "yes", "是")
        except EOFError:
            return False

    registry = ToolRegistry(workspace=workspace, confirm_callback=confirm)
    register_builtin_tools(registry)

    # Meta tools (self-discovery / tool search / install / self-authoring)
    from core.meta_tools import register_meta_tools, register_dynamic_tools
    from core.system_control import register_system_control_tools
    from core.web_tools import register_web_tools
    from core.weather_tools import register_weather_tools
    from core.sandbox import register_sandbox_tools
    from core.external_apis import register_external_api_tools
    from core.autonomy import register_autonomy_tools
    register_meta_tools(registry, confirm=confirm)
    register_system_control_tools(registry, confirm=confirm)
    register_web_tools(registry, confirm=confirm)
    register_weather_tools(registry, confirm=confirm)
    register_sandbox_tools(registry, confirm=confirm)
    register_external_api_tools(registry, confirm=confirm)
    register_autonomy_tools(registry)
    register_dynamic_tools(registry, confirm=confirm)

    # Skills
    skills = load_skills(SKILLS_DIR)
    register_skill_tools(registry, skills)
    skills_prompt = build_skills_prompt(skills)
    if skills:
        print(f"[Skill] 加载 {len(skills)} 个技能：{', '.join(s['name'] for s in skills)}")

    # MCP
    clients = load_mcp_servers(MCP_PATH)
    if clients:
        register_mcp_tools(registry, clients)

    model = args.model or cfg["model"]
    agent_config = AgentConfig(
        max_steps=args.max_steps or cfg.get("max_steps", 30),
        max_context_messages=cfg.get("max_context_messages", 16),
        verbose=not args.quiet,
    )
    agent = Agent(provider, registry, config=agent_config)
    agent.set_model(model)
    agent.messages.append({"role": "system", "content": build_system_prompt(registry, skills_prompt)})
    return agent, provider, workspace


BANNER = r"""
  ___  _   _ ___ ___   _   _  _   _   _ _____ ___ ___  _   _
 / _ \| | | |_ _/ _ \ /_\ | \| | /_\ | |_   _|_ _/ _ \| \ | |
| (_) | |_| || | (_) / _ \| .` |/ _ \| | | |  | | (_) |  \| |
 \___/ \___/|___\___/_/ \_\_|\_/_/ \_\_| |_| |___\___/|_|\__|
"""

HELP = """
命令：
  /help          显示帮助
  /model <名字>  切换模型（如 /model qwen3.5:4b）
  /ws <路径>     切换工作空间
  /clear         清空对话历史
  /tools         列出可用工具
  /models        列出本地模型
  /quit          退出
"""


def interactive(agent, provider, workspace):
    print(f"\n工作空间: {workspace}")
    print(f"当前模型: {agent.current_model}")
    print("输入 /help 看命令，直接输入需求开始干活。\n")

    while True:
        try:
            user_input = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not user_input:
            continue

        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            if cmd in ("/quit", "/exit", "/q"):
                print("再见！")
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/model":
                if not arg:
                    print(f"当前模型: {agent.current_model}")
                else:
                    agent.set_model(arg)
                    print(f"已切换到: {arg}")
            elif cmd == "/ws":
                if not arg:
                    print(f"当前工作空间: {agent.registry.workspace}")
                else:
                    try:
                        agent.registry.set_workspace(arg)
                        print(f"已切换工作空间: {agent.registry.workspace}")
                    except ValueError as e:
                        print(f"错误: {e}")
            elif cmd == "/clear":
                agent.reset(agent.messages[0]["content"] if agent.messages else "")
                print("对话历史已清空")
            elif cmd == "/tools":
                for n in agent.registry.list_names():
                    print("  -", n)
            elif cmd == "/models":
                for m in provider.list_models():
                    print("  -", m)
            else:
                print(f"未知命令: {cmd}")
            continue

        try:
            answer = agent.run(user_input)
            print(f"\n[Agent] {answer}\n")
        except KeyboardInterrupt:
            print("\n[已中断]")
        except Exception as e:
            print(f"\n[错误] {type(e).__name__}: {e}\n")


def main():
    parser = argparse.ArgumentParser(description="本地 Agent")
    parser.add_argument("-w", "--workspace", help="工作空间目录")
    parser.add_argument("-m", "--model", help="使用的模型名")
    parser.add_argument("-p", "--prompt", help="单次提问，执行完退出")
    parser.add_argument("--max-steps", type=int, help="最大步数")
    parser.add_argument("--quiet", action="store_true", help="精简输出")
    args = parser.parse_args()

    print(BANNER)
    print("本地 AI Agent  ·  工具调用 · Skill · MCP\n")
    cfg = load_config()
    agent, provider, workspace = make_agent(args, cfg)

    if args.prompt:
        answer = agent.run(args.prompt)
        print(f"\n{answer}")
    else:
        interactive(agent, provider, workspace)


if __name__ == "__main__":
    main()
