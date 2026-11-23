"""工具系统：Agent 的"手"。

内置工具 + MCP 工具统一注册到这里，模型看到的是同一套 function schema。
所有文件操作限定在工作空间内，命令行执行有安全确认钩子。
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
import base64
import json
import shutil
from pathlib import Path


class ToolResult:
    def __init__(self, ok: bool, output: str):
        self.ok = ok
        self.output = output

    def __str__(self):
        return self.output


class ToolRegistry:
    """工具注册表。模型通过 name 找到工具执行。"""

    def __init__(self, workspace: str | None = None, confirm_callback=None,
                 allow_outside_workspace: bool = False):
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        self.confirm = confirm_callback
        self.allow_outside_workspace = allow_outside_workspace
        self._tools: dict[str, dict] = {}   # name -> {"schema":..., "handler":..., "source":...}

    def register(self, name: str, description: str, parameters: dict,
                 handler, source: str = "builtin"):
        self._tools[name] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            "handler": handler,
            "source": source,
        }

    def get_schemas(self, sources: set[str] | None = None) -> list[dict]:
        """给模型看的工具列表。sources 用于过滤（builtin / mcp:xxx / skill:xxx）。"""
        return [
            t["schema"] for t in self._tools.values()
            if sources is None or t["source"] in sources or t["source"].split(":")[0] in sources
        ]

    def execute(self, name: str, arguments: dict) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(False, f"错误：工具 '{name}' 不存在。可用工具：{list(self._tools)}")
        try:
            return tool["handler"](arguments)
        except Exception as e:
            return ToolResult(False, f"工具 '{name}' 执行失败：{type(e).__name__}: {e}")

    def list_names(self) -> list[str]:
        return list(self._tools)

    def set_workspace(self, path: str):
        p = Path(path).resolve()
        if not p.is_dir():
            raise ValueError(f"工作空间不是有效目录：{path}")
        self.workspace = p

    def _safe_path(self, path: str) -> Path:
        """把路径限制在工作空间内，防止模型乱读系统文件。"""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        p = p.resolve()
        try:
            p.relative_to(self.workspace)
        except ValueError:
            if not self.allow_outside_workspace:
                raise ValueError(f"路径 {path} 超出工作空间 {self.workspace}，已拒绝。可在设置中启用完整本地文件访问。")
        return p

    def display_path(self, path: str | Path) -> str:
        p = Path(path)
        try:
            return str(p.relative_to(self.workspace))
        except ValueError:
            return str(p)


def execute_shell_command(command: str, workspace: str | Path, shell: str = "CMD", timeout: int = 120):
    """Run a command in a hidden child process; workspace cwd is not an OS sandbox."""
    shell = (shell or "CMD").strip().upper()
    if os.name == "nt":
        if shell in ("POWERSHELL", "PS"):
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            shell = "CMD"
            argv = ["cmd.exe", "/d", "/s", "/c", command]
        proc = subprocess.run(argv, cwd=str(workspace), capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc = subprocess.run(command, shell=True, cwd=str(workspace), capture_output=True,
                              text=True, timeout=timeout, encoding="utf-8", errors="replace")
    output = (proc.stdout or "") + (proc.stderr or "")
    if len(output) > 20000:
        output = output[:20000] + "\n...[输出截断]"
    return proc.returncode, output, shell


def _find_visible_vscode_window():
    """Return a real visible VS Code window title, if one is present."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        titles = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if "Visual Studio Code" in title or title.endswith(" - Code"):
                titles.append(title)
                return False
            return True

        user32.EnumWindows(enum_proc(visit), 0)
        return titles[0] if titles else None
    except Exception:
        return None


def register_builtin_tools(registry: ToolRegistry, allow_dangerous: bool = False,
                           default_shell: str = "CMD", image_callback=None,
                           default_model: str | None = None, progress_callback=None):
    """注册内置工具：文件、目录、命令行。"""

    # ---- 读文件 ----
    def read_file(args):
        p = registry._safe_path(args.get("path", ""))
        if not p.exists():
            return ToolResult(False, f"文件不存在：{p}")
        if not p.is_file():
            return ToolResult(False, f"不是文件：{p}")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(False, f"读取失败：{e}")
        max_chars = 50000
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...[截断，共 {len(text)} 字符]"
        return ToolResult(True, f"文件 {registry.display_path(p)} 内容：\n{text}")

    registry.register(
        "read_file", "读取工作空间内的文本文件",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对工作空间的路径或绝对路径"}
        }, "required": ["path"]},
        read_file,
    )

    # ---- 写文件 ----
    def write_file(args):
        p = registry._safe_path(args.get("path", ""))
        content = args.get("content", "")
        try:
            p.relative_to(registry.workspace)
            outside_workspace = False
        except ValueError:
            outside_workspace = True
        if outside_workspace:
            if not registry.confirm:
                return ToolResult(False, "写入工作空间以外的文件需要用户确认；当前运行方式没有确认窗口，已拒绝。")
            if not registry.confirm(f"Agent 请求在工作空间以外创建或覆盖文件：\n{p}\n\n是否允许？"):
                return ToolResult(False, f"用户拒绝了外部文件写入：{p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(True, f"已写入：{registry.display_path(p)}（{len(content)} 字符）")

    registry.register(
        "write_file", "创建或覆盖工作空间内的文本文件",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对工作空间的路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        }, "required": ["path", "content"]},
        write_file,
    )

    # ---- 删除文件（总是向用户确认）----
    def delete_file(args):
        p = registry._safe_path(args.get("path", ""))
        if not p.exists():
            return ToolResult(False, f"文件不存在：{p}")
        if not p.is_file():
            return ToolResult(False, f"只允许删除单个文件，不能删除目录：{p}")
        if p == registry.workspace:
            return ToolResult(False, "拒绝删除工作空间根目录")
        if not registry.confirm:
            return ToolResult(False, "删除操作需要用户确认；当前运行方式没有确认窗口，已拒绝。")
        rel = registry.display_path(p)
        if not registry.confirm(f"Agent 请求永久删除工作空间内的文件：\n{rel}\n\n是否删除？"):
            return ToolResult(False, f"用户拒绝删除：{rel}")
        p.unlink()
        return ToolResult(True, f"已删除文件：{rel}")

    registry.register(
        "delete_file", "删除工作空间内的单个文件（每次都需要用户确认）",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对工作空间的文件路径"}
        }, "required": ["path"]},
        delete_file,
    )

    # ---- 列目录 ----
    def list_dir(args):
        p = registry._safe_path(args.get("path", "."))
        if not p.exists():
            return ToolResult(False, f"目录不存在：{p}")
        if not p.is_dir():
            return ToolResult(False, f"不是目录：{p}")
        items = []
        for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            try:
                size = child.stat().st_size if child.is_file() else 0
                items.append(f"{'[DIR] ' if child.is_dir() else '      '}{child.name}  {size}")
            except OSError:
                items.append(child.name)
        rel = registry.display_path(p)
        return ToolResult(True, f"目录 {rel} 内容：\n" + "\n".join(items))

    registry.register(
        "list_dir", "列出工作空间内某个目录的文件",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径，. 表示工作空间根目录"}
        }, "required": []},
        list_dir,
    )

    # ---- 搜索文件 ----
    def search_files(args):
        pattern = args.get("pattern", "*")
        root = registry._safe_path(args.get("path", "."))
        import fnmatch
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", "venv")]
            for fn in filenames:
                if fnmatch.fnmatch(fn, pattern):
                    full = Path(dirpath) / fn
                    hits.append(registry.display_path(full))
            if len(hits) >= 200:
                hits.append("...[结果过多，已截断]")
                break
        return ToolResult(True, f"搜索 '{pattern}' 找到 {len(hits)} 个文件：\n" + "\n".join(hits))

    registry.register(
        "search_files", "按文件名通配符搜索文件，例如 *.py",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "通配符，如 *.py 或 test_*.txt"},
            "path": {"type": "string", "description": "搜索起始目录"},
        }, "required": ["pattern"]},
        search_files,
    )

    # ---- 在文件里搜索内容 ----
    def grep(args):
        keyword = args.get("keyword", "")
        root = registry._safe_path(args.get("path", "."))
        results = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", "venv")]
            for fn in filenames:
                f = Path(dirpath) / fn
                try:
                    for n, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if keyword in line:
                            results.append(f"{registry.display_path(f)}:{n}: {line.strip()[:200]}")
                except (OSError, UnicodeError):
                    continue
            if len(results) >= 100:
                results.append("...[结果过多，已截断]")
                break
        return ToolResult(True, f"搜索 '{keyword}' 结果：\n" + ("\n".join(results) if results else "无匹配"))

    registry.register(
        "grep", "在文件内容中搜索关键词",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "要搜索的关键词"},
            "path": {"type": "string", "description": "搜索目录"},
        }, "required": ["keyword"]},
        grep,
    )

    # ---- 命令行 ----
    def run_command(args):
        command = args.get("command", "")
        shell = args.get("shell", default_shell)
        if not command.strip():
            return ToolResult(False, "命令为空")
        # Run in the selected workspace, but this is not an OS sandbox.
        # Require approval for every shell command; keyword filters alone are not a security boundary.
        if not allow_dangerous:
            if not registry.confirm:
                return ToolResult(False, "命令执行需要用户确认；当前运行方式没有确认窗口，已拒绝。")
            if not registry.confirm(f"Agent 请求在工作空间目录通过 {shell} 执行命令：\n{command}\n\n命令按当前 Windows 用户权限运行，是否允许？"):
                return ToolResult(False, "用户拒绝了命令执行")
        try:
            code, output, used_shell = execute_shell_command(command, registry.workspace, shell)
            return ToolResult(code == 0,
                              f"Shell：{used_shell}\n命令：{command}\n退出码：{code}\n输出：\n{output}")
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"命令超时（120秒）：{command}")

    registry.register(
        "run_command", "在工作空间目录后台执行 CMD/PowerShell 命令；GUI 可配置为自主执行或逐次确认。不是操作系统沙盒。",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "shell": {"type": "string", "enum": ["CMD", "PowerShell"], "description": "命令行类型；省略时使用当前默认值"},
        }, "required": ["command"]},
        run_command,
    )

    # ---- 工作空间信息 ----
    def workspace_info(args):
        os_name = platform.system()
        if os.name == "nt":
            version = sys.getwindowsversion()
            label = "Windows 11" if version.build >= 22000 else f"Windows {version.major}.{version.minor}"
            os_name = f"{label} (build {version.build})"
        return ToolResult(True, f"当前工作空间：{registry.workspace}\n"
                                f"操作系统：{os_name}\n架构：{platform.machine()}\n"
                                f"Python：{sys.version.split()[0]}")

    registry.register(
        "workspace_info", "查看当前工作空间路径和环境信息",
        {"type": "object", "properties": {}, "required": []},
        workspace_info,
    )
    registry.register(
        "system_info", "查看当前工作空间、Windows 版本和运行架构",
        {"type": "object", "properties": {}, "required": []}, workspace_info)

    # ---- ComfyUI 文生图 ----
    def generate_image(args):
        def report_progress(stage, percent, remaining=None):
            if progress_callback:
                progress_callback({"stage": stage, "percent": percent, "remaining": remaining})

        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult(False, "生图提示词不能为空")
        width = int(args.get("width", 768))
        height = int(args.get("height", 768))
        steps = int(args.get("steps", 8))
        if not registry.confirm:
            return ToolResult(False, "提交本地生图任务需要用户确认；当前运行方式没有确认窗口，已拒绝。")
        if not registry.confirm(
                f"Agent 请求使用本机 ComfyUI 生成图片：\n{prompt}\n\n"
                f"尺寸：{width}×{height}，步数：{steps}。为释放显存，会先卸载当前驻留的 Ollama 模型，"
                "下一轮对话时 Ollama 会重新加载。任务受显存/内存预检保护，是否提交？"):
            return ToolResult(False, "用户取消了生图任务")

        report_progress("准备生图与释放显存", 2, 150)

        ai_home = Path(__file__).resolve().parents[2]
        script = ai_home / "scripts" / "zimage_generate.py"
        if not script.is_file():
            return ToolResult(False, f"找不到 ComfyUI 生图脚本：{script}")
        python = Path(sys.executable)
        if python.stem.lower() == "pythonw":
            python = python.with_name("python.exe")
        command = [str(python), str(script), prompt, "--width", str(width),
                   "--height", str(height), "--steps", str(steps)]
        try:
            ollama = ai_home / "Ollama" / "ollama.exe"
            if ollama.is_file():
                try:
                    request = urllib.request.Request("http://127.0.0.1:11434/api/ps", method="GET")
                    with urllib.request.urlopen(request, timeout=5) as response:
                        loaded = json.loads(response.read().decode("utf-8")).get("models", [])
                    for item in loaded:
                        name = item.get("name")
                        if name:
                            report_progress("卸载 Ollama 模型，释放显存", 5, 145)
                            subprocess.run([str(ollama), "stop", name], cwd=str(ai_home),
                                           capture_output=True, text=True, timeout=30,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    pass
            try:
                with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2):
                    pass
            except Exception:
                comfy_dir = ai_home / "ComfyUI"
                main_py = comfy_dir / "main.py"
                if not main_py.is_file():
                    return ToolResult(False, f"ComfyUI 启动文件不存在：{main_py}")
                report_progress("正在启动 ComfyUI", 8, 140)
                env = os.environ.copy()
                env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                log_path = ai_home / "logs_comfy_agent.log"
                with log_path.open("ab") as log_file:
                    subprocess.Popen(
                        [str(python), str(main_py), "--port", "8188", "--lowvram",
                         "--cpu-vae", "--reserve-vram", "0.5"],
                        cwd=str(comfy_dir), env=env, stdout=log_file, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                ready = False
                for _ in range(120):
                    time.sleep(1)
                    if progress_callback and _ % 5 == 0:
                        report_progress("等待 ComfyUI 就绪", min(18, 8 + _ // 12), 135 - _)
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2):
                            ready = True
                            break
                    except Exception:
                        continue
                if not ready:
                    return ToolResult(False, f"ComfyUI 启动超时，请检查日志：{log_path}")
            report_progress("向 ComfyUI 提交图片任务", 20, 90)
            proc = subprocess.Popen(command, cwd=str(ai_home), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            output_lines = []

            def read_output():
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    if line.startswith("AGENT_PROGRESS:"):
                        try:
                            report = json.loads(line[len("AGENT_PROGRESS:"):])
                            if progress_callback:
                                progress_callback(report)
                        except (ValueError, TypeError):
                            output_lines.append(line)
                    else:
                        output_lines.append(line)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                returncode = proc.wait(timeout=900)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                reader.join(timeout=3)
                return ToolResult(False, "本地生图等待超过 15 分钟；ComfyUI 可能仍在运行，请检查 ComfyUI/output。")
            reader.join(timeout=3)
        except subprocess.TimeoutExpired:
            return ToolResult(False, "本地生图等待超过 15 分钟；ComfyUI 可能仍在运行，请检查 ComfyUI/output。")
        output = "\n".join(output_lines)
        if returncode:
            return ToolResult(False, f"ComfyUI 生图失败（退出码 {returncode}）：\n{output[-4000:]}")
        image_paths = [line.removeprefix("IMAGE_PATH:").strip()
                       for line in output.splitlines() if line.startswith("IMAGE_PATH:")]
        if not image_paths:
            return ToolResult(False, f"ComfyUI 已结束但没有返回图片路径：\n{output[-2000:]}")
        report_progress("图片完成", 100, 0)
        for path in image_paths:
            if image_callback:
                image_callback(path)
        return ToolResult(True, "本地图片已生成：\n" + "\n".join(image_paths))

    registry.register(
        "generate_image", "使用本机 ComfyUI 的 Z-Image-Turbo 根据提示词生成图片；提交前会询问用户确认",
        {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "图片内容和风格描述"},
            "width": {"type": "integer", "description": "宽度，最大 1024，默认 768"},
            "height": {"type": "integer", "description": "高度，最大 1024，默认 768"},
            "steps": {"type": "integer", "description": "采样步数，最大 12，默认 8"},
        }, "required": ["prompt"]},
        generate_image,
    )

    # ---- 查询真实的即插即用设备 ----
    def query_devices(args):
        if os.name != "nt":
            return ToolResult(False, "当前设备查询实现只支持 Windows。")
        script = r'''
$ErrorActionPreference = 'Stop'
try {
  $devices = Get-PnpDevice -PresentOnly | Where-Object {
    $_.Class -in @('Display','Monitor','Printer','USB','Bluetooth','Keyboard','Mouse','DiskDrive','Camera','AudioEndpoint','Ports') -or
    $_.FriendlyName -match 'USB|Bluetooth|Keyboard|Mouse|Monitor|Display|Printer|Camera|Webcam|Headset|Headphones|Microphone|Storage|External'
  } | Select-Object @{n='type';e={$_.Class}}, @{n='name';e={$_.FriendlyName}}, @{n='status';e={$_.Status}}
} catch {
  $devices = Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode=0' | Where-Object {
    $_.PNPClass -in @('Display','Monitor','Printer','USB','Bluetooth','Keyboard','Mouse','DiskDrive','Camera','Ports') -or
    $_.Name -match 'USB|Bluetooth|Keyboard|Mouse|Monitor|Display|Printer|Camera|Webcam|Headset|Headphones|Microphone|Storage|External'
  } | Select-Object @{n='type';e={$_.PNPClass}}, @{n='name';e={$_.Name}}, @{n='status';e={'OK'}}
}
$json = ConvertTo-Json -InputObject @($devices) -Depth 3 -Compress
[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($json)))
'''
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                                     "-EncodedCommand", encoded], capture_output=True, text=True,
                                    timeout=30, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            return ToolResult(False, f"设备查询失败：{type(exc).__name__}: {exc}")
        if result.returncode:
            return ToolResult(False, f"设备查询失败：{(result.stderr or result.stdout)[-2000:]}")
        try:
            payload = base64.b64decode((result.stdout or "").strip()).decode("utf-16le")
            devices = json.loads(payload or "[]")
            if isinstance(devices, dict):
                devices = [devices]
        except ValueError:
            return ToolResult(False, f"Windows 返回了无法解析的设备清单：{result.stdout[-2000:]}")
        lines = [f"- {item.get('type') or '设备'}：{item.get('name')}（{item.get('status') or '状态未知'}）"
                 for item in devices if item.get("name")]
        return ToolResult(True, "Windows 即插即用设备清单（可能包含虚拟设备）：\n" +
                          ("\n".join(lines[:100]) if lines else "未发现匹配的设备。"))

    registry.register(
        "query_devices", "查询 Windows 当前连接/启用的即插即用设备；只读取设备名称、类别和状态",
        {"type": "object", "properties": {}, "required": []}, query_devices)

    # ---- 打开真实桌面应用 ----
    def open_application(args):
        name = str(args.get("name", "")).strip().lower()
        if name not in {"vscode", "explorer", "notepad"}:
            return ToolResult(False, "目前支持打开 vscode、explorer 或 notepad。")
        target = str(args.get("path", "")).strip()
        if target:
            try:
                target_path = registry._safe_path(target)
            except ValueError as exc:
                return ToolResult(False, str(exc))
            if not target_path.exists():
                return ToolResult(False, f"目标路径不存在：{target_path}")

        if os.name == "nt" and name == "vscode":
            candidates = [
                shutil.which("Code.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Microsoft VS Code", "Code.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Microsoft VS Code", "Code.exe"),
                shutil.which("code"),
            ]
            executable = next((Path(path) for path in candidates if path and Path(path).is_file()), None)
            if executable is None:
                return ToolResult(False, "找不到 VS Code。已检查 PATH 和常见安装目录；请安装 VS Code 或提供其 Code.exe 路径。")
            command = [str(executable), "--new-window"]
            if target:
                command.append(str(target_path))
            else:
                command.append(str(registry.workspace))
            try:
                process = subprocess.Popen(command, cwd=str(registry.workspace),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, close_fds=True)
            except Exception as exc:
                return ToolResult(False, f"找到 VS Code：{executable}\n启动失败：{type(exc).__name__}: {exc}")
            deadline = time.time() + 8
            visible_title = None
            while time.time() < deadline:
                visible_title = _find_visible_vscode_window()
                if visible_title:
                    break
                time.sleep(0.25)
            if visible_title:
                return ToolResult(True, f"已确认 VS Code 窗口可见：{visible_title}\n程序：{executable}\n"
                                  f"启动 PID：{process.pid}\n打开路径：{target_path if target else registry.workspace}")
            return ToolResult(False, f"已向 Windows 提交 VS Code 启动请求，但 8 秒内未检测到可见窗口。\n"
                              f"程序：{executable}\n启动 PID：{process.pid}\n"
                              "请检查 Windows 是否隐藏了窗口、是否已有 VS Code 进程阻塞；不要把进程已启动当作窗口已显示。")

        if os.name == "nt":
            if name == "explorer":
                executable = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "explorer.exe")
            else:
                executable = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "System32", "notepad.exe")
            command = [executable, str(target_path) if target else str(registry.workspace)]
        else:
            executable = shutil.which("notepad") if name == "notepad" else None
            if not executable:
                return ToolResult(False, f"当前平台未配置 {name} 的启动方式。")
            command = [executable, str(target_path)] if target else [executable]
        try:
            process = subprocess.Popen(command, cwd=str(registry.workspace),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, close_fds=True)
            return ToolResult(True, f"已启动 {name}，PID：{process.pid}。")
        except Exception as exc:
            return ToolResult(False, f"启动 {name} 失败：{type(exc).__name__}: {exc}")

    registry.register(
        "open_application", "启动本机桌面应用。可打开 VS Code，并可传入工作空间内的文件或目录路径；工具会返回真实程序路径和进程 ID",
        {"type": "object", "properties": {
            "name": {"type": "string", "enum": ["vscode", "explorer", "notepad"]},
            "path": {"type": "string", "description": "可选：工作空间内的文件或文件夹"},
        }, "required": ["name"]}, open_application)

    def open_vscode(args):
        return open_application({"name": "vscode", **args})

    registry.register(
        "open_vscode", "打开本机安装的 Visual Studio Code；默认打开当前工作空间。返回启动结果和 PID，不虚报窗口已显示",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "可选：工作空间内的文件或文件夹"},
        }, "required": []}, open_vscode)

    registry.register(
        "execute_command", "兼容常见模型命名的命令工具别名；在工作空间执行命令，每条都需用户确认",
        {"type": "object", "properties": {
            "command": {"type": "string"},
            "shell": {"type": "string", "enum": ["CMD", "PowerShell"]},
        }, "required": ["command"]}, run_command)
