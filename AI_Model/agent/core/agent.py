"""Agent 核心：ReAct 循环（思考 → 调工具 → 观察 → 再思考）。

这就是 Claude Code / Codex 的核心逻辑，用你本地模型驱动。
"""
from __future__ import annotations

import json
import inspect
import re
import time
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    max_steps: int = 30              # 最多跑多少步，防止无限循环
    max_context_messages: int = 16   # 保持短小模型的历史提示在合理范围内
    verbose: bool = True
    tools_enabled: bool = True
    step_delay: float = 0.2
    log_callback: object = None      # GUI 用：每步日志回调 fn(str)
    token_callback: object = None    # GUI 用：流式文本回调 fn(str)
    progress_callback: object = None # GUI 用：隐藏推理阶段的活动提示 fn(str)


class Agent:
    def __init__(self, provider, registry, system_prompt: str = "", config: AgentConfig | None = None):
        self.provider = provider
        self.registry = registry
        self.config = config or AgentConfig()
        self.messages: list[dict] = []
        self.current_model: str | None = None
        self.total_tokens = 0
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def set_model(self, model: str):
        self.current_model = model

    def reset(self, system_prompt: str = ""):
        self.messages = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def _log(self, msg: str):
        cb = getattr(self.config, "log_callback", None)
        if cb:
            try:
                cb(msg)
            except Exception:
                pass
        if self.config.verbose and not cb:
            print(msg, flush=True)

    def _trim_context(self):
        """上下文太长时保留 system + 最近的对话，防止爆上下文。"""
        max_n = self.config.max_context_messages
        if len(self.messages) <= max_n:
            return
        head = [m for m in self.messages if m.get("role") == "system"]
        tail = self.messages[-(max_n - len(head)):]
        self.messages = head + tail

    @staticmethod
    def _select_relevant_tools(schemas: list[dict], user_input: str) -> list[dict]:
        """Avoid sending large, unrelated MCP schemas to small local model contexts."""
        text = user_input.casefold()
        browser_intent = bool(re.search(r"浏览器|网页|网站|页面|browser|webpage|website|playwright|登录网页|打开链接", text))
        integration_intent = bool(re.search(r"github|git 仓库|git仓库|邮箱|邮件|mcp", text))
        selected = []
        for schema in schemas:
            function = schema.get("function", {})
            name = function.get("name", "")
            if not name.startswith("mcp_"):
                selected.append(schema)
                continue
            if not (browser_intent or integration_intent):
                continue
            description = function.get("description", "")
            match = re.search(r"\[MCP:([^\]]+)\]", description)
            server = (match.group(1) if match else name.split("_", 2)[1]).casefold()
            if server == "playwright" or "playwright" in server:
                if not browser_intent:
                    continue
                base = ("browser_navigate", "browser_snapshot", "browser_tabs", "browser_wait_for")
                suffix = name.split("playwright_", 1)[-1]
                keep = any(suffix.endswith(part) for part in base)
                if re.search(r"点击|单击|click|按钮", text):
                    keep |= suffix.endswith("browser_click")
                if re.search(r"输入|填写|搜索|搜索网页|type|fill", text):
                    keep |= suffix.endswith(("browser_type", "browser_fill_form", "browser_press_key"))
                if re.search(r"截图|拍屏|screenshot", text):
                    keep |= suffix.endswith("browser_take_screenshot")
                if re.search(r"上传文件|上传图片|file upload", text):
                    keep |= suffix.endswith("browser_file_upload")
                if re.search(r"查找|页面里找|find text", text):
                    keep |= suffix.endswith("browser_find")
                if keep:
                    selected.append(schema)
                continue
            server_aliases = {
                "github": ("github", "git"),
                "email": ("邮箱", "邮件", "email", "mail"),
                "gmail": ("邮箱", "邮件", "gmail", "email"),
            }
            aliases = server_aliases.get(server, (server,))
            if integration_intent and any(alias in text for alias in aliases):
                selected.append(schema)
        return selected

    @staticmethod
    def _needs_deep_reasoning(user_input: str) -> bool:
        return bool(re.search(
            r"为什么|原因|分析|比较|评估|规划|设计方案|推导|复杂|逐一|多步骤|排错|调试|修复|实现|编写|审查|优化|总结并",
            user_input,
        ))

    def _build_tool_result_message(self, call: dict, result) -> dict:
        """兼容两种模型：标准 tool_calls 用 role=tool，JSON 格式用 user 消息回灌。"""
        call_id = call.get("id", "")
        text = result.output if hasattr(result, "output") else str(result)
        if call_id.startswith("json_"):
            # qwen2.5-coder 非标准输出：把工具结果作为普通 user 消息喂回去
            return {
                "role": "user",
                "content": f"[工具 {call['name']} 的执行结果]\n{text}\n\n请继续完成任务，"
                           f"如果完成就直接给出最终答案。",
            }
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": text,
        }

    def _build_assistant_message(self, answer: dict, calls: list[dict]) -> dict:
        """把模型回复写回消息历史，兼容标准和 JSON 两种工具调用。"""
        content = answer.get("content") or ""
        if calls and any(c.get("id", "").startswith("json_") for c in calls):
            # 非标准：模型输出的是 JSON 文本，原样保留 content
            return {"role": "assistant", "content": content or json.dumps(
                [{"name": c["name"], "arguments": c["arguments"]} for c in calls],
                ensure_ascii=False)}
        if calls:
            return {
                "role": "assistant",
                "content": content,
                "tool_calls": [{
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                    },
                } for c in calls],
            }
        return {"role": "assistant", "content": content}

    def run(self, user_input: str, images: list[str] | None = None) -> str:
        """跑一次完整任务，返回最终答案。"""
        user_message = {"role": "user", "content": user_input}
        if images:
            user_message["image_paths"] = images
        self.messages.append(user_message)
        tools = (self._select_relevant_tools(self.registry.get_schemas(), user_input)
                 if self.config.tools_enabled else [])

        repeated_call = None
        repeat_count = 0
        for step in range(1, self.config.max_steps + 1):
            self._trim_context()
            self._log(f"\n--- 第 {step}/{self.config.max_steps} 步 ---")

            chat_kwargs = {
                "messages": self.messages,
                "tools": tools if tools else None,
                "model": self.current_model,
            }
            model_name = (self.current_model or "").casefold()
            thought_mode = self._needs_deep_reasoning(user_input)
            if "deepseek-r1" in model_name or "qwen3.5" in model_name:
                chat_kwargs["think"] = thought_mode
            token_cb = getattr(self.config, "token_callback", None)
            progress_cb = getattr(self.config, "progress_callback", None)
            if token_cb:
                try:
                    token_cb(None)
                except Exception:
                    pass
                try:
                    signature = inspect.signature(self.provider.chat)
                    accepts_callback = "on_token" in signature.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in signature.parameters.values())
                except (TypeError, ValueError):
                    accepts_callback = False
                if accepts_callback:
                    chat_kwargs["on_token"] = token_cb
                    if progress_cb and ("on_progress" in signature.parameters or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in signature.parameters.values())):
                        chat_kwargs["on_progress"] = progress_cb
            answer = self.provider.chat(**chat_kwargs)
            calls = answer.get("tool_calls", [])
            content = answer.get("content") or ""
            if not calls and not content and chat_kwargs.get("think") is True:
                # Thinking models can consume their output budget before emitting an answer.
                self._log("模型本轮只有思考内容；关闭思考重试一次，避免空白回答。")
                chat_kwargs["think"] = False
                answer = self.provider.chat(**chat_kwargs)
                calls = answer.get("tool_calls", [])
                content = answer.get("content") or ""

            if not calls:
                # 没有工具调用 = 模型认为任务完成
                self.messages.append({"role": "assistant", "content": content})
                return content or "模型没有生成可见回答。可以重试，或切换到 qwen2.5-coder:7b。"

            # 打印模型想法
            if content:
                self._log(f"思考: {content[:300]}")

            # 写回 assistant 消息（含工具调用）
            # 关键：标准格式必须用 Ollama 返回的原始 message，原样回灌。
            # 自己重建会丢掉 index 等字段、或把 arguments 二次序列化导致 400。
            raw_msg = answer.get("raw_message")
            if raw_msg and not any(c.get("id", "").startswith("json_") for c in calls):
                self.messages.append(dict(raw_msg))
            else:
                self.messages.append(self._build_assistant_message(answer, calls))

            # 执行所有工具调用
            for call in calls:
                name = call["name"]
                args = call["arguments"]
                signature = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
                if signature == repeated_call:
                    repeat_count += 1
                else:
                    repeated_call, repeat_count = signature, 1
                if repeat_count >= 4:
                    return f"已停止：模型连续 {repeat_count} 次重复调用 {name}，没有根据工具结果调整。请补充更明确的路径或换一个模型后重试。"
                self._log(f"调用工具: {name}({json.dumps(args, ensure_ascii=False)[:200]})")
                result = self.registry.execute(name, args)
                self._log(f"结果: {str(result)[:500]}")
                self.messages.append(self._build_tool_result_message(call, result))

            if self.config.step_delay:
                time.sleep(self.config.step_delay)

        return f"达到最大步数 {self.config.max_steps}，任务未完成。可继续对话让它接着做。"
