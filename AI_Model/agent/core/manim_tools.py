"""Manim（3b1b 原版 ManimGL）动画工具 —— 让 Agent 能自己写脚本渲染数学动画并预览。

集成内容：
- 引擎：tools/manim（github.com/3b1b/manim，ManimGL），专用环境 venv-manim。
- 线代源码：tools/manim_videos/_2016/eola（Essence of Linear Algebra 全 16 章）。

诚实边界：
- 3b1b/videos 是老版 Manim API（OldTexText、CONFIG 字典等），与当前 ManimGL 不兼容，
  直接渲染大概率报错。工具会如实返回真实报错，不假装成功。
- Agent 自己写的脚本用当前 ManimGL API（from manimlib import *），渲染成功率高。
- manimgl 依赖 OpenGL/系统图形库；无头环境可能只能 -w 写文件模式。
- 渲染耗时，工具设超时上限，避免卡死（资源守护已在别处常驻）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent          # agent/
AI_MODEL = BASE.parent                                  # AI_Model/
MANIM_DIR = AI_MODEL / "tools" / "manim"
VIDEOS_DIR = AI_MODEL / "tools" / "manim_videos" / "_2016" / "eola"
VENV_MANIM = AI_MODEL / "venv-manim"
MANIM_PY = VENV_MANIM / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SCRIPTS_DIR = BASE / "data" / "manim_scripts"
OUTPUT_DIR = BASE / "data" / "manim_output"

MANIM_PATTERNS = re.compile(
    r"manim|动画|3b1b|线性代数.*(?:动画|视频)|数学动画|eola|essence of linear|渲染.*视频", re.I)


def _env_ok():
    if not MANIM_DIR.exists():
        return False, f"Manim 引擎未安装：{MANIM_DIR}"
    if not MANIM_PY.exists():
        return False, f"Manim 专用环境不存在：{MANIM_PY}"
    return True, ""


def _list_examples(a):
    if not VIDEOS_DIR.exists():
        return ToolResult(False, f"线代源码目录不存在：{VIDEOS_DIR}")
    files = sorted(VIDEOS_DIR.glob("chapter*.py"))
    lines = ["3b1b《线性代数的本质》场景源码（tools/manim_videos/_2016/eola）：", ""]
    for f in files:
        # 提取场景类名
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            classes = re.findall(r"^class\s+(\w+)\s*\(\s*(\w+)\s*\)", text, re.M)
            scenes = [f"{c}({b})" for c, b in classes if "Scene" in b]
            lines.append(f"- {f.name}：{', '.join(scenes[:8])}{' ...' if len(scenes) > 8 else ''}")
        except Exception:
            lines.append(f"- {f.name}")
    lines += ["", f"共 {len(files)} 个章节文件。",
              "注意：这些是 2016 年老版 Manim API（OldTexText/CONFIG 等），与当前 ManimGL 不兼容，"
              "直接渲染会报真实错误。把它们当作『场景思路/动画逻辑参考』，新动画请用当前 API 自己写脚本。"]
    return ToolResult(True, "\n".join(lines)[:12000])


def _render(a, preview_cb=None):
    ok, why = _env_ok()
    if not ok:
        return ToolResult(False, why)
    scene = (a.get("scene") or "").strip()
    code = (a.get("code") or "").strip()
    script_path = (a.get("script") or "").strip()
    resolution = (a.get("resolution") or "medium").lower()
    preview = bool(a.get("preview", True))

    # 优先级：code（写到临时脚本）> script（指定已有文件）
    target = None
    if code:
        if not scene:
            return ToolResult(False, "提供 code 时必须同时提供 scene（要渲染的场景类名）。")
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_]", "_", scene)[:40]
        target = SCRIPTS_DIR / f"{safe}.py"
        target.write_text(code, encoding="utf-8")
    elif script_path:
        target = Path(script_path).expanduser()
        if not target.is_absolute():
            target = AI_MODEL / target
        if not target.exists():
            return ToolResult(False, f"脚本不存在：{script_path}")
    else:
        return ToolResult(False, "需要 code（Python 脚本内容）+ scene，或 script（已有脚本路径）。")

    if not scene:
        return ToolResult(False, "需要 scene（场景类名）。")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    res_flag = {"low": "-l", "medium": "-m", "high": "--hd", "4k": "--uhd"}.get(resolution, "-m")
    # manimgl <script> <SceneClass> -w 写文件（无头友好）
    # ffmpeg 绝对路径已写入 tools/manim/custom_config.yml，cwd=MANIM_DIR 时自动加载
    cmd = [str(MANIM_PY), "-m", "manimlib", str(target), scene, "-w", res_flag]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              cwd=str(MANIM_DIR), encoding="utf-8", errors="replace")
        out = (proc.stdout or "")[-3000:]
        err = (proc.stderr or "")[-3000:]
        if proc.returncode != 0:
            return ToolResult(False, f"Manim 渲染失败（真实报错，未伪装成功）：\n{err or out}")
        # 找生成的最新 mp4（ManimGL 默认输出到 tools/manim/videos/）
        videos_dir = MANIM_DIR / "videos"
        candidates = list(videos_dir.glob(f"{scene}.mp4")) if videos_dir.exists() else []
        candidates += list(OUTPUT_DIR.glob(f"{scene}*.mp4"))
        if not candidates:
            return ToolResult(True, f"渲染命令已执行（退出码 0），但未找到 mp4。输出：\n{out[-1000:]}。\n"
                              f"请检查 {videos_dir}。")
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        msg = f"动画渲染完成：{newest}\n分辨率={resolution}，脚本={target.name}"
        if preview and preview_cb:
            try:
                preview_cb(str(newest))
                msg += "，已请求预览。"
            except Exception as exc:
                msg += f"（预览失败：{exc}）"
        return ToolResult(True, msg)
    except subprocess.TimeoutExpired:
        return ToolResult(False, "渲染超过 300 秒已中止（防止卡死）。请降低分辨率或简化场景。"
                          "动画渲染很吃资源，建议开着资源守护。")
    except Exception as exc:
        return ToolResult(False, f"渲染失败：{type(exc).__name__}: {exc}")


def register_manim_tools(registry, preview_cb=None):
    ok, why = _env_ok()
    registry.register(
        "manim_status",
        "检查 Manim 动画环境是否可用（引擎、venv-manim、线代源码）。做动画前可先调用。",
        {"type": "object", "properties": {}},
        lambda a: ToolResult(ok, "Manim 环境就绪。\n引擎：" + str(MANIM_DIR) +
                             f"\n线代源码：{len(list(VIDEOS_DIR.glob('chapter*.py')))} 个章节"
                             if ok else why), source="manim")

    registry.register(
        "manim_examples",
        "列出 3b1b《线性代数的本质》全部场景源码（类名+章节）。用户要做线代动画、参考 3b1b 风格时用。"
        "注意老代码 API 不兼容，仅作思路参考。",
        {"type": "object", "properties": {}},
        _list_examples, source="manim")

    registry.register(
        "manim_render",
        "用 3b1b 原版 ManimGL 渲染数学动画并预览。用户提供动画描述时，自己写 from manimlib import * 的"
        "Python 脚本(code)，定义 Scene 子类，然后传 scene 类名渲染。参数 code+scene(或 script+scene)、"
        "resolution(low/medium/high)、preview。渲染成功返回 mp4 路径并可弹窗预览；失败返回真实报错。",
        {"type": "object", "properties": {
            "code": {"type": "string", "description": "ManimGL Python 脚本内容（from manimlib import *）"},
            "scene": {"type": "string", "description": "要渲染的场景类名"},
            "script": {"type": "string", "description": "或：已有脚本文件路径"},
            "resolution": {"type": "string", "description": "low/medium/high/4k，默认 medium"},
            "preview": {"type": "boolean", "description": "是否弹窗预览，默认 true"}
        }},
        lambda a: _render(a, preview_cb), source="manim")


def has_manim_intent(text: str) -> bool:
    return bool(MANIM_PATTERNS.search(text))
