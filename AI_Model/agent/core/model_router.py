"""多模型协同 —— 让当前模型把子任务交给另一个本地模型处理（A 想出办法，B 有能力执行）。

能力矩阵（基于本项目实测，见 probe_result.json）：
- qwen3.5:4b   通用/工具调用强，主力
- qwen2.5-coder:7b  写代码/算法强
- deepseek-r1:8b  深度推理/数学强（thinking 慢）
- qwen2.5vl:7b  看图，不支持 tools，只能视觉聊天

工具：
- list_models_capability：列出本机模型和各自擅长领域。
- delegate_to_model：把一段子任务交给指定模型独立处理，拿回它的回答。
诚实边界：这是"顺序委托"（A 调 B 一次拿结果），不是多模型并行讨论；每个模型独立上下文，
B 看不到 A 的内部推理，只收到 A 传过去的 task 文本。是否会委托由当前模型自己决定。
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

from .tools import ToolResult

DEFAULT_BASE = "http://127.0.0.1:11434"

KNOWN_CAPABILITIES = {
    "qwen3.5:4b": "通用对话、工具调用、中文，主力模型，速度快",
    "qwen2.5-coder:7b": "写代码、调试、算法、代码审查最强",
    "deepseek-r1:8b": "深度推理、数学、复杂逻辑（会先思考，慢）",
    "qwen2.5vl:7b": "看图、读图、图表解读；不支持工具调用，只能视觉问答",
}


def _base_url() -> str:
    """优先从 agent/config.json 读 base_url。"""
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return str(data.get("base_url", DEFAULT_BASE)).rstrip("/")
    except Exception:
        return DEFAULT_BASE


def list_local_models() -> list[str]:
    try:
        with urllib.request.urlopen(_base_url() + "/api/tags", timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _list_with_capability(a):
    models = list_local_models()
    if not models:
        return ToolResult(False, "无法连接本地 Ollama，未找到已安装模型。请先启动 Ollama。")
    lines = ["本机可用模型与擅长领域：", ""]
    for m in models:
        cap = KNOWN_CAPABILITIES.get(m, "未标注（通用）")
        lines.append(f"- {m}：{cap}")
    lines += ["", "用 delegate_to_model(model=..., task=...) 把子任务交给更合适的模型。"]
    return ToolResult(True, "\n".join(lines))


def _delegate(a):
    model = (a.get("model") or "").strip()
    task = (a.get("task") or "").strip()
    system = (a.get("system") or "你是一个本地 AI 助手，用中文简洁、准确地完成任务。").strip()
    if not model or not task:
        return ToolResult(False, "需要 model（目标模型名）和 task（交给它完成的任务描述）。")
    if not re.fullmatch(r"[A-Za-z0-9_.:/\-]{1,80}", model):
        return ToolResult(False, "model 名不合法。")
    models = list_local_models()
    if models and model not in models:
        return ToolResult(False, f"模型 {model} 不在本机已装列表：{', '.join(models)}")
    cap = KNOWN_CAPABILITIES.get(model, "")
    if "不支持工具" in cap:
        # 视觉模型仍能纯文本回答，但提醒限制
        note = "（注意：该模型不支持工具调用，只能基于已有信息回答）"
    else:
        note = ""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 2048, "num_ctx": 8192},
    }
    try:
        req = urllib.request.Request(
            _base_url() + "/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = (data.get("message") or {}).get("content") or ""
        if not answer:
            return ToolResult(False, f"模型 {model} 返回空内容（可能 thinking 耗尽 token）。{note}")
        return ToolResult(True, f"【{model} 的回答】{note}\n\n{answer}")
    except Exception as exc:
        return ToolResult(False, f"委托 {model} 失败：{type(exc).__name__}: {exc}")


def register_model_router(registry):
    registry.register(
        "list_models_capability",
        "列出本机已安装的 AI 模型及各自擅长领域。用户问有什么模型、哪个模型更强、能否协同，"
        "或你想把任务交给别的模型前，先用它确认可用模型。",
        {"type": "object", "properties": {}},
        _list_with_capability, source="router")

    registry.register(
        "delegate_to_model",
        "把某个子任务交给另一个本地模型独立完成，拿回它的回答。当当前任务用别的模型更合适时用："
        "写代码→qwen2.5-coder:7b，深度推理/数学→deepseek-r1:8b，看图→qwen2.5vl:7b，通用→qwen3.5:4b。"
        "参数 model 目标模型名、task 完整任务描述、system 可选角色设定。"
        "注意：目标模型只收到 task 文本，看不到你的推理，要把上下文写清楚。",
        {"type": "object", "properties": {
            "model": {"type": "string", "description": "目标模型名，如 qwen2.5-coder:7b"},
            "task": {"type": "string", "description": "交给目标模型的完整任务描述"},
            "system": {"type": "string", "description": "可选：给目标模型的角色设定"}
        }, "required": ["model", "task"]},
        _delegate, source="router")


def has_delegate_intent(text: str) -> bool:
    return bool(re.search(
        r"协同|交给.*模型|让.*模型|换个模型|委托|哪个模型.*(?:擅长|强)|模型.*能力|多模型", text))
