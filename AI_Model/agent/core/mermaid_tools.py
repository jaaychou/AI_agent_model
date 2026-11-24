"""Mermaid 流程图渲染 —— 把 ```mermaid 代码块渲染成 PNG 供聊天内联预览。

渲染通道（按优先级）：
1. 本地 mermaid-cli (mmdc) —— 若用户装了 npx @mermaid-js/mermaid-cli，最可靠、离线。
2. 在线 Kroki (kroki.io) —— 开源图表服务，POST 渲染，无需本地重依赖。
   流程图内容是用户自己让 Agent 画的示意图，不含敏感数据；若涉及隐私请启用本地 mmdc。

两者都失败时返回明确错误，GUI 保留原始代码并显示提示，绝不假装渲染成功。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "data" / "mermaid"


def _local_mmdc(code: str, out: Path) -> str | None:
    """尝试用本地 mmdc 渲染，成功返回 None，失败返回原因。"""
    try:
        npx = Path("D:/Program Files/nodejs/npx.cmd")
        cmd = [str(npx), "-y", "@mermaid-js/mermaid-cli", "-i", "-", "-o", str(out)]
        r = subprocess.run(cmd, input=code, capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0 and out.exists():
            return None
        return (r.stderr or r.stdout or "mmdc 执行失败")[:300]
    except FileNotFoundError:
        return "未找到 npx/node"
    except subprocess.TimeoutExpired:
        return "本地渲染超时(120s)"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _kroki(code: str, out: Path) -> str | None:
    """用 Kroki 在线服务渲染 Mermaid，成功返回 None。"""
    try:
        import urllib.request
        payload = code.encode("utf-8")
        req = urllib.request.Request(
            "https://kroki.io/mermaid/png", data=payload,
            headers={"Content-Type": "text/plain; charset=utf-8",
                     "User-Agent": "LocalAIAgent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data.startswith(b"\x89PNG"):
            return "Kroki 返回的不是 PNG"
        out.write_bytes(data)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def render_mermaid(code: str, filename: str = "diagram") -> ToolResult:
    """渲染 Mermaid 代码为 PNG，返回文件路径。

    默认用在线 Kroki（快、无需本地重依赖）。只有设置环境变量
    MERMAID_USE_LOCAL=1 时才走本地 mmdc——因为 npx -y 首次会下载大包，
    若每次都先试本地会拖慢渲染，故默认关闭。
    """
    code = (code or "").strip()
    if not code:
        return ToolResult(False, "Mermaid 代码为空。")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{filename}.png"
    local_err = ""
    if os.getenv("MERMAID_USE_LOCAL") == "1":
        local_err = _local_mmdc(code, out) or ""
        if not local_err:
            return ToolResult(True, f"已用本地 mermaid-cli 渲染：{out}")
    err = _kroki(code, out)
    if err is None:
        return ToolResult(True, f"已用 Kroki 渲染：{out}")
    suffix = f"（本地 mmdc：{local_err}）" if local_err else ""
    return ToolResult(False, f"Mermaid 渲染失败：{err}{suffix}。已保留原始代码。")


def register_mermaid_tools(registry):
    registry.register(
        "render_mermaid",
        "把 Mermaid 流程图代码渲染成 PNG 图片。用户要流程图/时序图/架构图且需要图片时用；"
        "优先本地渲染，失败用在线服务。返回图片路径供预览。",
        {"type": "object", "properties": {
            "code": {"type": "string", "description": "Mermaid 代码，如 graph TD; A-->B"},
            "filename": {"type": "string", "description": "输出文件名（可选）"}
        }, "required": ["code"]},
        lambda a: render_mermaid(a.get("code", ""), a.get("filename", "diagram")),
        source="mermaid")
