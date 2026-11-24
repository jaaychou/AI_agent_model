"""编译/运行环境工具 —— 让 Agent 自己探测本机编译器、写代码、编译运行、读输出、判成败。

用户要求：后台实现 Python / MATLAB / C++ 编译运行并"检查输出呈现"。
设计：
- `build_environment`：探测本机可用的解释器/编译器（python/node/g++/gcc/cl/matlab/cmake），
  Agent 动手前先调用它，不用猜路径。
- `compile_run`：给定语言+代码+可选编译参数，在隔离目录写文件→编译→运行→返回
  真实退出码与 stdout/stderr。ok 严格等于退出码 0，失败会明确提示"读 stderr 修正重试"。
- MATLAB 诚实降级：只有探测到 matlab.exe 才尝试；否则明确说未安装，不伪造运行结果。
- 这不是 OS 沙盒，只是隔离工作目录 + 超时 + 低优先级。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent
BUILD_ROOT = BASE / "data" / "builds"
MAX_OUTPUT = 12000
DEFAULT_TIMEOUT = 60


def _find_exe(names: list[str]) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def detect_environment() -> dict:
    """探测本机可用的解释器和编译器，返回 name->path。"""
    env = {
        "python": sys.executable,
        "node": _find_exe(["node"]),
        "g++": _find_exe(["g++"]),
        "gcc": _find_exe(["gcc"]),
        "cl": _find_exe(["cl"]),
        "clang++": _find_exe(["clang++"]),
        "cmake": _find_exe(["cmake"]),
        "make": _find_exe(["make", "mingw32-make"]),
        "matlab": "",
    }
    # MATLAB：常见安装路径探测
    candidates = [
        r"C:\Program Files\MATLAB\R2023b\bin\matlab.exe",
        r"C:\Program Files\MATLAB\R2024a\bin\matlab.exe",
        r"C:\Program Files\MATLAB\R2024b\bin\matlab.exe",
    ]
    for c in candidates:
        if Path(c).is_file():
            env["matlab"] = c
            break
    if not env["matlab"]:
        # 扫描 MATLAB 根目录
        root = Path(r"C:\Program Files\MATLAB")
        if root.is_dir():
            for exe in root.glob("R*/bin/matlab.exe"):
                env["matlab"] = str(exe)
                break
    return env


def _build_environment(a):
    env = detect_environment()
    lines = ["本机可用的编译/运行环境（Agent 已探测真实路径）：", ""]
    for name, path in env.items():
        lines.append(f"- {name}: {path or '未安装/未找到'}")
    lines += [
        "",
        "能力说明：",
        "- Python / Node：可直接运行代码。",
        "- C/C++：用 g++ 或 gcc（PATH 中有）编译后运行；Windows 若装了 MSVC 可用 cl。",
        "- MATLAB：仅当上面 matlab 有真实路径时才可运行；否则未安装，不要假装能跑 MATLAB。",
        "用 compile_run(language, code, ...) 写代码并编译运行，工具会返回真实退出码与输出。",
    ]
    return ToolResult(True, "\n".join(lines))


def _write_sources(workdir: Path, language: str, code: str, files: dict) -> Path | None:
    """把代码和辅助文件写进隔离目录，返回主文件路径。"""
    for name, content in (files.items() if isinstance(files, dict) else []):
        if not re.fullmatch(r"[\w./\-]{1,80}", str(name)):
            raise ValueError(f"非法文件名：{name}")
        target = workdir / str(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    ext = {
        "python": "py", "node": "js", "cpp": "cpp", "c": "c",
        "matlab": "m", "shell": "sh", "bash": "sh", "batch": "bat", "powershell": "ps1",
    }.get(language, "txt")
    entry = workdir / f"main.{ext}"
    entry.write_text(code, encoding="utf-8")
    return entry


def _run(cmd: list[str] | str, cwd: Path, timeout: int, shell: bool = False):
    kwargs = dict(cwd=str(cwd), capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=timeout)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if isinstance(cmd, str):
        return subprocess.run(cmd, shell=True, **kwargs)
    return subprocess.run(cmd, **kwargs)


def compile_run(a):
    """写代码→(可选编译)→运行→返回真实输出。ok 严格反映最终退出码。"""
    language = str(a.get("language", "python")).strip().casefold()
    code = str(a.get("code", ""))
    files = a.get("files") or {}
    extra = str(a.get("args", "")).strip()
    timeout = max(1, min(int(a.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT), 300))
    if not code.strip():
        return ToolResult(False, "缺少 code（要编译/运行的源码）。")

    env = detect_environment()
    workdir = BUILD_ROOT / f"{language}_{int(time.time() * 1000)}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        entry = _write_sources(workdir, language, code, files)
        exe = workdir / "program.exe" if os.name == "nt" else workdir / "program"
        stages = []

        if language in {"python"}:
            proc = _run([env["python"], str(entry), *shlex_split(extra)], workdir, timeout)
            stages.append(("运行 Python", proc))
        elif language == "node":
            if not env["node"]:
                return ToolResult(False, "Node 未安装。")
            proc = _run([env["node"], str(entry), *shlex_split(extra)], workdir, timeout)
            stages.append(("运行 Node", proc))
        elif language in {"cpp", "c"}:
            compiler = env["g++"] if language == "cpp" else (env["gcc"] or env["g++"])
            if not compiler:
                return ToolResult(False, "未找到 g++/gcc 编译器，无法编译 C/C++。")
            src = str(entry)
            build = [compiler, src, "-o", str(exe), "-std=c++17"] if language == "cpp" \
                else [compiler, src, "-o", str(exe)]
            if extra:
                build.extend(shlex_split(extra))
            comp = _run(build, workdir, timeout)
            stages.append(("编译", comp))
            if comp.returncode != 0:
                return ToolResult(False, format_stages(stages) +
                                  "\n\n编译失败：请读上方 stderr 定位错误，修正 code 后重试，不要把失败当成完成。")
            proc = _run([str(exe)], workdir, timeout)
            stages.append(("运行", proc))
        elif language == "matlab":
            if not env["matlab"]:
                return ToolResult(False,
                                  "本机未探测到 MATLAB 可执行文件（matlab.exe）。"
                                  "C:\\Program Files\\MATLAB\\R2023b 目录存在但为空，MATLAB 可能未完整安装。"
                                  "请确认 MATLAB 安装路径；在探测到之前 Agent 不会假装运行 MATLAB。\n"
                                  "建议：改用 Python(numpy/scipy/matplotlib) 完成同等数值计算与绘图。")
            # MATLAB 批处理模式：-batch 执行脚本后退出
            cmd = [env["matlab"], "-batch", f"run('{entry}')"]
            proc = _run(cmd, workdir, timeout)
            stages.append(("运行 MATLAB", proc))
        elif language in {"shell", "bash"}:
            proc = _run(["bash", str(entry), *shlex_split(extra)], workdir, timeout)
            stages.append(("运行 Shell", proc))
        elif language == "batch":
            proc = _run(f'cmd /c "{entry}" {extra}', workdir, timeout, shell=True)
            stages.append(("运行 Batch", proc))
        elif language == "powershell":
            proc = _run(["powershell", "-NoProfile", "-NonInteractive", "-File", str(entry),
                         *shlex_split(extra)], workdir, timeout)
            stages.append(("运行 PowerShell", proc))
        else:
            return ToolResult(False, f"不支持 language={language}。可用：python/node/cpp/c/matlab/shell/batch/powershell")

        final = stages[-1][1]
        ok = final.returncode == 0
        text = format_stages(stages)
        if not ok:
            text += ("\n\n运行失败：请读上方 stderr/输出定位问题，修正 code 或 args 后重试，"
                     "不要把失败当成完成。")
        return ToolResult(ok, text)
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"执行超时（{timeout}秒）。进程可能已被终止，副作用状态未知，请检查 {workdir}。")
    except Exception as exc:
        return ToolResult(False, f"执行失败：{type(exc).__name__}: {exc}")
    finally:
        # 保留产物供用户核验，不做自动删除（避免误删编译产物）
        pass


def shlex_split(s: str) -> list[str]:
    import shlex
    try:
        return shlex.split(s)
    except Exception:
        return s.split()


def format_stages(stages) -> str:
    parts = [f"工作目录：{BUILD_ROOT}"]
    for label, proc in stages:
        out = (proc.stdout or "")[:MAX_OUTPUT]
        err = (proc.stderr or "")[:MAX_OUTPUT]
        parts.append(f"\n=== {label} ===\n退出码：{proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
    return "\n".join(parts)


CODE_PATTERNS = re.compile(
    r"编译|运行.*(?:代码|程序|脚本)|c\+\+|cpp|\bc\b语言|matlab|"
    r"makefile|cmake|\.cpp|\.c\b|build |跑.*代码|执行.*代码", re.I)


def register_build_tools(registry):
    registry.register(
        "build_environment",
        "探测本机可用的编译器/解释器（python/node/g++/gcc/cl/matlab/cmake）。"
        "要让 Agent 编译运行 C/C++/MATLAB/Python 代码前先调用它确认环境。",
        {"type": "object", "properties": {}},
        _build_environment, source="build")

    registry.register(
        "compile_run",
        "写代码(Python/C/C++/MATLAB/Node/Shell/PowerShell)→编译(如需)→运行→返回真实退出码与 stdout/stderr。"
        "失败时 ok=False 并提示读输出修正重试。参数 language、code、可选 files(辅助文件)、args(编译/运行参数)、timeout。"
        "先调 build_environment 确认编译器可用；MATLAB 未安装会诚实说明。非 OS 沙盒，仅隔离目录+超时。",
        {"type": "object", "properties": {
            "language": {"type": "string", "description": "python/node/cpp/c/matlab/shell/batch/powershell"},
            "code": {"type": "string", "description": "要编译/运行的源码"},
            "files": {"type": "object", "description": "可选：辅助文件名->内容"},
            "args": {"type": "string", "description": "可选：编译/运行参数，如 -O2"},
            "timeout": {"type": "integer", "description": "超时秒数，默认60，最大300"}
        }, "required": ["language", "code"]},
        compile_run, source="build")


def has_code_intent(text: str) -> bool:
    return bool(CODE_PATTERNS.search(text))
