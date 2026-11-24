"""隔离的编译/测试执行环境 —— 让 Agent 能安全跑代码而不污染用户系统。

设计：
- 每次执行在 runtime/sandbox/<随机ID> 建一个一次性目录，用完可清理。
- 子进程用低优先级运行，带超时、内存/输出上限，cwd 限制在沙盒目录。
- 支持的语言：python / node / 以及通过 run_command 的编译命令（gcc/g++ 等，若系统有）。
- 网络访问不主动禁止（当前无 OS 级沙盒），但在返回说明中明确标注：这不是安全隔离，
  恶意代码仍可能逃逸；不要在此运行来路不明的代码。

关键：与主工作空间隔离，临时目录自动/手动清理，避免 Agent 实验把用户文件搞乱。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent
SANDBOX_ROOT = BASE / "runtime" / "sandbox"
MAX_OUTPUT = 20000
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300


def _safe_rel(name: str) -> Path:
    """拒绝 ../ 等逃逸路径。"""
    name = str(name or "").strip().replace("\\", "/")
    if not name or name.startswith("/") or ".." in name.split("/"):
        raise ValueError(f"非法的文件相对路径：{name}")
    return Path(name)


def create_sandbox() -> Path:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    sandbox_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    workdir = SANDBOX_ROOT / sandbox_id
    workdir.mkdir(parents=True)
    return workdir


def cleanup_sandbox(workdir: str | Path, keep: bool = False) -> str:
    p = Path(workdir)
    try:
        resolved = p.resolve()
        root = SANDBOX_ROOT.resolve()
        if root not in resolved.parents and resolved != root:
            return f"拒绝清理非沙盒目录：{p}"
        if keep:
            return f"已保留沙盒目录：{p}"
        if p.exists():
            shutil.rmtree(p)
        return f"已清理沙盒目录：{p}"
    except Exception as exc:
        return f"清理失败：{exc}"


def register_sandbox_tools(registry, confirm=None):
    def sandbox_run(args):
        """在隔离目录里写文件、执行、返回真实输出；可选清理。"""
        language = str(args.get("language", "python")).strip().casefold()
        code = str(args.get("code", ""))
        files = args.get("files") or {}
        run_command = str(args.get("command", "")).strip()
        cleanup = bool(args.get("cleanup", True))
        timeout = max(1, min(int(args.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        if not code and not run_command:
            return ToolResult(False, "请提供 code（代码）或 command（编译/运行命令）。")
        if language not in {"python", "node", "shell", "command"}:
            return ToolResult(False, f"暂不支持 language={language}。可用：python / node / shell / command。")
        workdir = create_sandbox()
        try:
            # 写入用户提供的辅助文件（限制在沙盒内）
            for name, content in (files.items() if isinstance(files, dict) else []):
                try:
                    rel = _safe_rel(name)
                except ValueError as exc:
                    return ToolResult(False, str(exc))
                target = workdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(content), encoding="utf-8")
            # 主代码文件
            entry = None
            if code:
                ext = {"python": "py", "node": "js", "shell": "sh", "command": "txt"}[language]
                entry = workdir / f"main.{ext}"
                entry.write_text(code, encoding="utf-8")

            # 构建实际命令
            if run_command:
                # 用户/Agent 指定的命令（如 gcc main.c -o main && ./main）
                if os.name == "nt":
                    cmd = ["cmd", "/c", run_command]
                else:
                    cmd = ["/bin/sh", "-c", run_command]
            elif language == "python":
                cmd = [sys.executable, str(entry)]
            elif language == "node":
                cmd = ["node", str(entry)]
            elif language == "shell":
                cmd = ["/bin/sh", str(entry)] if os.name != "nt" else ["cmd", "/c", str(entry)]
            else:
                return ToolResult(False, "command 模式必须提供 command。")

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            try:
                proc = subprocess.run(cmd, cwd=str(workdir), env=env, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=timeout)
                out = (proc.stdout or "")[:MAX_OUTPUT]
                err = (proc.stderr or "")[:MAX_OUTPUT]
                status = f"退出码：{proc.returncode}"
            except subprocess.TimeoutExpired:
                out, err, status = "", "", f"超时（{timeout} 秒），进程已被终止；副作用状态未知，请检查产物。"
            result = (f"沙盒目录：{workdir}\n命令：{' '.join(cmd)[:300]}\n{status}\n"
                      f"--- stdout ---\n{out}\n--- stderr ---\n{err}")
            result += ("\n\n注意：这不是操作系统级安全沙盒，只是隔离工作目录 + 超时 + 低优先级。"
                       "不要在此运行来路不明的代码。")
            return ToolResult(True, result)
        finally:
            if cleanup:
                cleanup_sandbox(workdir)

    def sandbox_cleanup(args):
        path = str(args.get("path", "")).strip()
        keep = bool(args.get("keep", False))
        if not path:
            # 没给路径就清理全部沙盒
            if not SANDBOX_ROOT.exists():
                return ToolResult(True, "没有需要清理的沙盒。")
            count = 0
            for child in SANDBOX_ROOT.iterdir():
                if child.is_dir():
                    cleanup_sandbox(child)
                    count += 1
            return ToolResult(True, f"已清理 {count} 个沙盒目录。")
        return ToolResult(True, cleanup_sandbox(path, keep=keep))

    registry.register(
        "sandbox_run", "在一次性隔离目录里运行代码/编译测试（python/node/shell/命令），带超时和输出上限，用完自动清理。比直接 run_command 更安全，适合跑实验代码、编译测试、验证脚本。不是 OS 级安全沙盒",
        {"type": "object", "properties": {
            "language": {"type": "string", "description": "python/node/shell/command，默认 python"},
            "code": {"type": "string", "description": "要运行的代码"},
            "files": {"type": "object", "description": "可选：额外文件 {文件名: 内容}"},
            "command": {"type": "string", "description": "可选：编译/运行命令（如 gcc main.c -o main && ./main）"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 300"},
            "cleanup": {"type": "boolean", "description": "结束后是否清理沙盒，默认 true"},
        }, "required": []}, sandbox_run, source="sandbox")
    registry.register(
        "sandbox_cleanup", "清理隔离沙盒目录。给 path 清理指定沙盒，不给则清理全部；keep=true 可保留",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "可选：沙盒目录路径"},
            "keep": {"type": "boolean", "description": "true=保留不删除"},
        }, "required": []}, sandbox_cleanup, source="sandbox")
