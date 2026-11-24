"""元工具（meta-tools）——让 Agent 拥有"找工具、装工具、造工具"的自我扩展能力。

设计目标：
- discover_tools：让模型随时看清自己当前拥有哪些工具（含动态加载的）。
- search_tools：联网搜索能解决当前问题的 Python 包 / CLI 工具（PyPI 官方 API）。
- install_package：把 PyPI 包装进隔离目录，不改全局环境，可拆卸。
- load_python_tool / create_tool：把一个本地 .py 文件（或模型刚写出的代码）注册成新工具，
  下次对话继续可用。工具脚本约定一个 ``def run(args: dict) -> str`` 函数。
- inspect_environment：查看本机 Python 环境、已装包、PATH 上的可执行命令，
  让模型知道"手上现成有什么"。

安全边界（重要）：
- 所有安装都进 agent/runtime/vendor 隔离目录，不污染用户的 venv-comfy / 全局 Python。
- 动态工具必须显式加载 + 确认，加载前做静态检查（拒绝危险 import/eval/网络反弹等）。
- 模型自己写工具是"高级能力"，始终走 confirm 回调，用户可见可控。
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent          # agent/
RUNTIME = BASE / "runtime"
VENDOR = RUNTIME / "vendor"                            # pip --target 安装目录
DYNAMIC_TOOLS = BASE / "dynamic_tools"                 # 用户/模型自定义工具脚本
TOOL_INDEX = RUNTIME / "tool_index.json"               # 动态工具索引（持久化）

# 工具脚本里禁止的调用：防止"造工具"变成绕过确认的后门。
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
_FORBIDDEN_MODULES = {"ctypes", "subprocess", "socket", "multiprocessing", "pickle", "webbrowser"}


def ensure_runtime():
    VENDOR.mkdir(parents=True, exist_ok=True)
    DYNAMIC_TOOLS.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)


def current_python() -> str:
    return sys.executable


def _vendor_on_path():
    p = str(VENDOR)
    if p not in sys.path:
        sys.path.insert(0, p)


def load_installed_packages(limit: int = 200) -> list[dict]:
    """列出隔离目录里已装的包（来自 tool_index.json）。"""
    if not TOOL_INDEX.exists():
        return []
    try:
        data = json.loads(TOOL_INDEX.read_text(encoding="utf-8"))
        packages = data.get("packages", [])
        return packages[:limit]
    except Exception:
        return []


def save_package_record(name: str, version: str, source: str):
    data = {"packages": [], "tools": []}
    if TOOL_INDEX.exists():
        try:
            data = json.loads(TOOL_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.setdefault("packages", [])
    data["packages"] = [p for p in data["packages"] if p.get("name", "").lower() != name.lower()]
    data["packages"].insert(0, {
        "name": name, "version": version, "source": source,
        "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    TOOL_INDEX.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def register_dynamic_tools(registry, confirm=None):
    """启动时把持久化的动态工具重新注册进注册表。返回注册的名字列表。"""
    ensure_runtime()
    _vendor_on_path()
    if not TOOL_INDEX.exists():
        return []
    try:
        tools = json.loads(TOOL_INDEX.read_text(encoding="utf-8")).get("tools", [])
    except Exception:
        return []
    registered = []
    for entry in tools:
        name = entry.get("name")
        path = Path(entry.get("path", ""))
        if not name or not path.exists():
            continue
        try:
            _register_tool_file(registry, name, path, entry.get("description", ""), confirm=confirm,
                                trusted=True)  # 已加载过的工具视为可信，不再重复确认
            registered.append(name)
        except Exception as exc:
            print(f"[meta] 动态工具 {name} 加载失败：{exc}")
    return registered


def _static_check_tool_source(code: str) -> str | None:
    """返回 None 表示通过；否则返回拒绝原因。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"语法错误：{exc}"
    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
                return f"禁止调用 {func.id}（可通过普通 Python 语义实现，绝不能绕过确认）"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            else:
                names = [(node.module or "").split(".")[0]]
            for mod in names:
                if mod in _FORBIDDEN_MODULES:
                    return f"工具脚本禁止直接 import {mod}。需要子进程/网络的场景请用内置 run_command / web_search 工具。"
    if not has_run:
        return "工具脚本必须定义 def run(args: dict) -> str 函数。"
    return None


def _register_tool_file(registry, name: str, path: Path, description: str,
                        confirm=None, trusted: bool = False):
    """把一个 .py 工具脚本真正注册进 ToolRegistry。"""
    code = path.read_text(encoding="utf-8")
    if not trusted:
        reason = _static_check_tool_source(code)
        if reason:
            raise ValueError(reason)
    spec = importlib.util.spec_from_file_location(f"dyn_{name}_{uuid.uuid4().hex[:6]}", path)
    if spec is None or spec.loader is None:
        raise ValueError("无法加载工具模块")
    module = importlib.util.module_from_spec(spec)
    _vendor_on_path()
    spec.loader.exec_module(module)
    run = getattr(module, "run", None)
    if not callable(run):
        raise ValueError("工具脚本缺少可调用的 run(args) 函数")
    meta = getattr(module, "TOOL_META", {}) or {}
    parameters = meta.get("parameters") or {"type": "object", "properties": {}, "required": []}
    desc = meta.get("description") or description or f"动态工具：{name}"

    def handler(args):
        try:
            result = run(args or {})
        except Exception as exc:
            return ToolResult(False, f"工具 {name} 运行失败：{type(exc).__name__}: {exc}")
        if isinstance(result, ToolResult):
            return result
        return ToolResult(True, str(result))

    registry.register(name, desc, parameters, handler, source=f"dynamic:{path.name}")
    return desc, parameters


def _pypi_package_info(name: str) -> dict | None:
    """查询单个包的官方 JSON 元数据（PyPI JSON API 稳定可用）。"""
    url = f"https://pypi.org/pypi/{quote(name)}/json"
    req = Request(url, headers={"User-Agent": "LocalAgent/1.0 (tool discovery)"})
    try:
        with urlopen(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    info = data.get("info", {})
    return {"name": info.get("name", name), "version": info.get("version", ""),
            "summary": info.get("summary", "") or ""}


def _pypi_search(query: str, limit: int = 5) -> list[dict]:
    """找 PyPI 包：先用 Bing 搜 site:pypi.org 候选名，再逐个用官方 JSON API 确认。"""
    import urllib.parse
    from html import unescape

    # 1) 从 Bing 结果里提取 pypi.org/project/<name> 候选
    candidates: list[str] = []
    bing_url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": f"site:pypi.org/project {query}", "format": "rss", "setlang": "en"})
    try:
        req = Request(bing_url, headers={"User-Agent": "Mozilla/5.0 LocalAgent/1.0"})
        with urlopen(req, timeout=15) as response:
            xml = response.read().decode("utf-8", "replace")
        for link in re.findall(r"<link>([^<]+)</link>", xml):
            m = re.search(r"pypi\.org/project/([A-Za-z0-9_.-]+)/?", unescape(link))
            if m:
                pkg = m.group(1)
                if pkg not in candidates:
                    candidates.append(pkg)
    except Exception:
        pass

    # 2) 没有 Bing 结果时，用查询词本身当候选名试几个常见变体
    if not candidates:
        base = re.sub(r"[^a-z0-9]", "-", query.strip().casefold()).strip("-")
        candidates = [c for c in {query.strip().casefold(), base, base.replace("-", "")} if c]

    # 3) 逐个用官方 JSON API 验证真实存在并拿简介
    results = []
    for name in candidates[:limit + 4]:
        info = _pypi_package_info(name)
        if info:
            results.append(info)
            if len(results) >= limit:
                break
    return results


def register_meta_tools(registry, confirm=None):
    ensure_runtime()
    _vendor_on_path()

    def discover_tools(args):
        names = registry.list_names()
        groups = {}
        for name in names:
            tool = registry._tools.get(name, {})
            source = tool.get("source", "builtin")
            groups.setdefault(source, []).append(name)
        lines = [f"当前共 {len(names)} 个可用工具："]
        for source, items in sorted(groups.items()):
            lines.append(f"\n[{source}] ({len(items)})")
            for item in sorted(items):
                schema = registry._tools[item]["schema"]["function"]
                desc = schema.get("description", "")
                lines.append(f"  - {item}: {desc[:120]}")
        dynamic = load_installed_packages()
        if dynamic:
            lines.append(f"\n已装进隔离目录的包：{', '.join(p['name'] for p in dynamic)}")
        lines.append("\n如果现有工具都不解决问题，可用 search_tools 找新工具、install_package 安装、"
                     "create_tool 自己写一个工具；也可用 run_command 调用 PATH 上的命令行程序。")
        return ToolResult(True, "\n".join(lines))

    def search_tools(args):
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(False, "请给出要搜索的工具/功能关键词，例如 'pdf ocr' 或 'serial port'。")
        try:
            results = _pypi_search(query)
        except (URLError, TimeoutError, OSError) as exc:
            return ToolResult(False, f"联网搜索 PyPI 失败：{exc}。可改用 web_search 搜索，或说明无法核实。")
        if not results:
            return ToolResult(True, f"PyPI 未找到与 '{query}' 明显相关的包。可换关键词，或用 web_search 搜索其他来源。")
        lines = [f"PyPI 搜索 '{query}' 结果（安装请用 install_package）："]
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. {item['name']} {item['version']} — {item['summary'][:160]}")
        return ToolResult(True, "\n".join(lines))

    def install_package(args):
        package = str(args.get("package", "")).strip()
        version = str(args.get("version", "")).strip()
        if not package or not re.fullmatch(r"[A-Za-z0-9_.\-]+", package):
            return ToolResult(False, "包名不合法，只能是字母数字、下划线、点、连字符。")
        spec = f"{package}=={version}" if version and re.fullmatch(r"[0-9A-Za-z.\-]+", version) else package
        if confirm and not confirm(f"Agent 请求安装 Python 包「{spec}」到隔离目录 {VENDOR}（不改动全局环境）。是否允许？"):
            return ToolResult(False, "用户拒绝安装包。")
        cmd = [current_python(), "-m", "pip", "install", "--disable-pip-version-check",
               "--target", str(VENDOR), spec]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(BASE))
        except subprocess.TimeoutExpired:
            return ToolResult(False, "安装超时（300 秒），请稍后重试或换包。")
        output = (proc.stdout or "")[-2500:] + ("\n" + (proc.stderr or "")[-1500:] if proc.stderr else "")
        if proc.returncode != 0:
            return ToolResult(False, f"pip 安装失败（退出码 {proc.returncode}）：\n{output}")
        save_package_record(package, version or "latest", "pypi")
        _vendor_on_path()
        return ToolResult(True, f"已把 {spec} 装进隔离目录 {VENDOR}。\n{output}\n"
                                "现在可以 create_tool 写工具来调用它，或直接 import 试试。")

    def inspect_environment(args):
        py = current_python()
        try:
            ver = subprocess.run([py, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            ver = py
        lines = [f"Python 解释器：{py}（{ver}）",
                 f"隔离包目录：{VENDOR}",
                 f"自定义工具目录：{DYNAMIC_TOOLS}"]
        modules = {}
        for mod in ("PIL", "pypdf", "docx", "openpyxl", "requests", "bs4", "fitz", "serial", "pyautogui"):
            try:
                spec = importlib.util.find_spec(mod)
                modules[mod] = "已安装" if spec else "未安装"
            except Exception:
                modules[mod] = "未知"
        lines.append("关键库可用性：" + "，".join(f"{k}={v}" for k, v in modules.items()))
        lines.append("PATH 上的常用命令：" + "，".join(
            f"{name}={shutil.which(name) or '无'}"
            for name in ("python", "pip", "git", "node", "npm", "ffmpeg", "magick", "tesseract")))
        return ToolResult(True, "\n".join(lines))

    def create_tool(args):
        name = str(args.get("name", "")).strip()
        code = str(args.get("code", ""))
        description = str(args.get("description", "")).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,39}", name):
            return ToolResult(False, "工具名不合法：需 2-40 位字母/数字/下划线，且以字母开头。")
        reason = _static_check_tool_source(code)
        if reason:
            return ToolResult(False, f"工具脚本未通过安全检查：{reason}")
        if confirm and not confirm(f"Agent 想创建一个新工具「{name}」并永久加入工具箱。代码将保存到 {DYNAMIC_TOOLS}。是否允许？"):
            return ToolResult(False, "用户拒绝创建新工具。")
        ensure_runtime()
        path = DYNAMIC_TOOLS / f"{name}.py"
        path.write_text(code, encoding="utf-8")
        try:
            desc, params = _register_tool_file(registry, name, path, description, confirm=confirm)
        except Exception as exc:
            path.unlink(missing_ok=True)
            return ToolResult(False, f"注册工具失败：{exc}")
        data = {"packages": load_installed_packages(), "tools": []}
        if TOOL_INDEX.exists():
            try:
                data = json.loads(TOOL_INDEX.read_text(encoding="utf-8"))
            except Exception:
                pass
        data.setdefault("tools", [])
        data["tools"] = [t for t in data["tools"] if t.get("name") != name]
        data["tools"].append({"name": name, "path": str(path), "description": desc,
                              "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                              "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]})
        TOOL_INDEX.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(True, f"工具「{name}」已创建并注册，本次及以后对话都可直接调用。\n描述：{desc}\n路径：{path}")

    def write_tool_script(args):
        """让用户把写好的工具脚本放到指定目录，返回路径（不自动注册，需 create_tool/重启加载）。"""
        name = str(args.get("name", "")).strip()
        code = str(args.get("code", ""))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,39}", name):
            return ToolResult(False, "工具名不合法。")
        if confirm and not confirm(f"Agent 请求保存工具脚本 {name}.py 到 {DYNAMIC_TOOLS}。是否允许？"):
            return ToolResult(False, "用户拒绝保存工具脚本。")
        ensure_runtime()
        path = DYNAMIC_TOOLS / f"{name}.py"
        path.write_text(code, encoding="utf-8")
        return ToolResult(True, f"已保存：{path}。用 create_tool 注册，或重启后自动加载。")

    registry.register(
        "discover_tools", "列出当前 Agent 拥有的全部工具（含动态创建/安装的）及其能力描述。当不确定自己能不能做某件事时先调用它",
        {"type": "object", "properties": {}, "required": []}, discover_tools, source="meta")
    registry.register(
        "inspect_environment", "查看本机 Python 解释器、隔离包目录、关键库是否可用、PATH 上的命令行工具。用于判断手上现成有什么能力",
        {"type": "object", "properties": {}, "required": []}, inspect_environment, source="meta")
    registry.register(
        "search_tools", "联网搜索 PyPI 上能解决问题的 Python 工具/库，返回包名、版本和简介。遇到现有工具解决不了的任务时先搜这里",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "要找的工具/功能关键词，如 'pdf ocr'、'excel'"},
        }, "required": ["query"]}, search_tools, source="meta")
    registry.register(
        "install_package", "把 PyPI 包装进隔离目录（不改全局环境）。安装后需 create_tool 写成工具或直接 import 使用；会请求用户确认",
        {"type": "object", "properties": {
            "package": {"type": "string", "description": "包名"},
            "version": {"type": "string", "description": "可选版本号"},
        }, "required": ["package"]}, install_package, source="meta")
    registry.register(
        "create_tool", "把一段 Python 代码注册成一个新的持久工具（脚本必须含 def run(args: dict) -> str，可选 TOOL_META）。这是 Agent 自己造工具的核心能力，会请求用户确认",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "工具名，如 'image_resizer'"},
            "description": {"type": "string", "description": "这个工具做什么"},
            "code": {"type": "string", "description": "完整 Python 脚本，定义 run(args) 函数"},
        }, "required": ["name", "description", "code"]}, create_tool, source="meta")
    registry.register(
        "write_tool_script", "仅保存工具脚本文件到自定义工具目录，不立即注册（用于用户自己管理）。真正加入工具箱请用 create_tool",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "脚本名"},
            "code": {"type": "string", "description": "Python 代码"},
        }, "required": ["name", "code"]}, write_tool_script, source="meta")
