"""Approximate elapsed-time and ETA helpers for local model tasks."""
from __future__ import annotations

import re


def classify_task(prompt: str) -> str:
    if re.search(r"生图|生成.{0,12}(?:图|照片|插画)|画.{0,12}(?:图|图片|插画)|小猫的照片", prompt):
        return "image"
    if re.search(r"代码|程序|脚本|编写|写一个|修复|调试|实现|函数|class |def |python|javascript|typescript", prompt, re.I):
        return "code"
    if re.search(r"运行|执行|创建|删除|安装|配置|Git|仓库|打开|启动", prompt, re.I):
        return "action"
    return "chat"


_BASE_SECONDS = {
    "deepseek-r1:8b": {"chat": 35, "code": 150, "action": 90, "image": 180},
    "qwen3.5:4b": {"chat": 16, "code": 75, "action": 55, "image": 150},
    "qwen2.5-coder:7b": {"chat": 12, "code": 60, "action": 45, "image": 150},
    "qwen2.5vl:7b": {"chat": 14, "code": 60, "action": 45, "image": 150},
}


def expected_seconds(model: str, category: str, samples: dict | None = None) -> float:
    key = f"{model}|{category}"
    values = (samples or {}).get(key, [])
    if values:
        return max(3.0, sum(float(value) for value in values[-5:]) / len(values[-5:]))
    defaults = _BASE_SECONDS.get(model, {})
    return float(defaults.get(category, _BASE_SECONDS["qwen3.5:4b"][category]))


def estimated_progress(elapsed: float, estimate: float) -> tuple[int, int]:
    """Return approximate percentage and ETA seconds; 100 is reserved for completion."""
    elapsed, estimate = max(0.0, elapsed), max(1.0, estimate)
    if elapsed <= estimate:
        percent = min(95, int(elapsed / estimate * 95))
        remaining = max(1, round(estimate - elapsed))
    else:
        # The baseline was exceeded: keep the bar short of completion and
        # project another small interval instead of claiming 100% or 0 seconds.
        percent = 95
        remaining = max(5, min(30, round((elapsed - estimate) * 0.5 + 5)))
    return percent, remaining


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    if minutes:
        return f"{minutes}分{seconds:02d}秒"
    return f"{seconds}秒"
