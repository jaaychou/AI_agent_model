"""工具系统：Agent 的"手"。

内置工具 + MCP 工具统一注册到这里，模型看到的是同一套 function schema。
所有文件操作限定在工作空间内，命令行执行有安全确认钩子。
"""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
import base64
import shutil
import tempfile
import zipfile
import posixpath
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree


class ToolResult:
    def __init__(self, ok: bool, output: str):
        self.ok = ok
        self.output = output

    def __str__(self):
        return self.output


def _extract_ooxml_text(path: Path, max_chars: int = 50000) -> str:
    """Extract readable text from DOCX/XLSX/PPTX containers without loading raw bytes into the model."""
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 5000 or sum(item.file_size for item in infos) > 100 * 1024 * 1024:
                raise ValueError("Office 文件展开后超过安全读取上限（5000 项/100 MiB）")
        from .rag import _read_document
        return "\n\n".join(text for _page, text in _read_document(path))[:max_chars]

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(item.file_size for item in infos) > 100 * 1024 * 1024:
            raise ValueError("Office 文件展开后超过安全读取上限（5000 项/100 MiB）")
        names = {item.filename for item in infos}
        if suffix in {".xlsx", ".xlsm"}:
            main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            doc_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
            strings = []
            if "xl/sharedStrings.xml" in names:
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                strings = ["".join(node.text or "" for node in item.findall(f".//{{{main_ns}}}t"))
                           for item in root.findall(f"{{{main_ns}}}si")]
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {item.attrib.get("Id"): item.attrib.get("Target", "")
                       for item in relationships.findall(f"{{{pkg_rel_ns}}}Relationship")}
            output = []
            for sheet in workbook.findall(f".//{{{main_ns}}}sheet"):
                title = sheet.attrib.get("name", "Sheet")
                relation_id = sheet.attrib.get(f"{{{doc_rel_ns}}}id")
                target = targets.get(relation_id, "")
                sheet_path = (target.lstrip("/") if target.startswith("/")
                              else posixpath.normpath(posixpath.join("xl", target)))
                if sheet_path not in names:
                    continue
                sheet_root = ElementTree.fromstring(archive.read(sheet_path))
                lines = [f"## {title}"]
                for row in sheet_root.findall(f".//{{{main_ns}}}row"):
                    values = []
                    for cell in row.findall(f"{{{main_ns}}}c"):
                        kind = cell.attrib.get("t", "")
                        value_node = cell.find(f"{{{main_ns}}}v")
                        value = value_node.text if value_node is not None and value_node.text else ""
                        if kind == "s" and value:
                            try:
                                value = strings[int(value)]
                            except (ValueError, IndexError):
                                pass
                        elif kind == "inlineStr":
                            value = "".join(node.text or "" for node in cell.findall(f".//{{{main_ns}}}t"))
                        values.append(value)
                    if any(values):
                        lines.append("\t".join(values))
                    if sum(len(line) + 1 for line in lines) >= max_chars:
                        break
                output.append("\n".join(lines))
            return "\n\n".join(output)[:max_chars]

        if suffix in {".pptx", ".pptm"}:
            drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
            slides = sorted((name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                            key=lambda name: int(re.search(r"slide(\d+)", name).group(1)))
            output = []
            for index, name in enumerate(slides, 1):
                root = ElementTree.fromstring(archive.read(name))
                paragraphs = []
                for paragraph in root.findall(f".//{{{drawing_ns}}}p"):
                    value = "".join(node.text or "" for node in paragraph.findall(f".//{{{drawing_ns}}}t"))
                    if value.strip():
                        paragraphs.append(value)
                output.append(f"## 第 {index} 张幻灯片\n" + "\n".join(paragraphs))
                if sum(len(part) for part in output) >= max_chars:
                    break
            return "\n\n".join(output)[:max_chars]

    raise ValueError(f"不是受支持的 Office Open XML 文件：{path.suffix}")


def _is_probably_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    allowed_controls = {7, 8, 9, 10, 12, 13, 27}
    controls = sum(byte < 32 and byte not in allowed_controls for byte in sample)
    return controls / len(sample) > 0.12


def _atomic_copy_file(source: Path, destination: Path) -> None:
    """Stream a file to a sibling temp file, then replace the destination atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                     dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _normalize_app_name(value: str) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def _is_subsequence(needle: str, haystack: str) -> bool:
    iterator = iter(haystack)
    return all(any(char == candidate for candidate in iterator) for char in needle)


def _rank_application_entries(query: str, shortcuts) -> list[Path]:
    normalized_query = _normalize_app_name(query)
    scored = []
    for shortcut in shortcuts:
        stem = Path(shortcut).stem
        normalized_stem = _normalize_app_name(stem)
        if len(normalized_stem) < 2:
            continue
        if normalized_stem in normalized_query:
            score = len(normalized_stem) * 100
        elif normalized_query and normalized_query in normalized_stem:
            score = len(normalized_query) * 50
        elif len(normalized_query) >= 4 and _is_subsequence(normalized_query, normalized_stem):
            score = len(normalized_query) * 10
        else:
            continue
        scored.append((score, Path(shortcut)))
    if not scored:
        return []
    highest = max(score for score, _path in scored)
    best = sorted({str(path): path for score, path in scored if score == highest}.values(),
                  key=lambda path: str(path).casefold())
    return best


def _find_start_menu_shortcuts(query: str) -> list[Path]:
    if os.name != "nt":
        return []
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    shortcuts = []
    for root in roots:
        if root.is_dir():
            try:
                shortcuts.extend(root.rglob("*.lnk"))
            except OSError:
                continue
    return _rank_application_entries(query, shortcuts)


def _find_path_executables(query: str) -> list[Path]:
    """Find PATH executables by display-name fragment or abbreviation."""
    if os.name != "nt":
        return []
    candidates = []
    for raw_root in os.environ.get("PATH", "").split(os.pathsep):
        root = Path(raw_root.strip().strip('"')) if raw_root.strip() else None
        if root is None or not root.is_dir():
            continue
        try:
            candidates.extend(path for path in root.iterdir()
                              if path.is_file() and path.suffix.casefold() == ".exe")
        except OSError:
            continue
    return _rank_application_entries(query, candidates)


def _find_registered_app_paths(query: str) -> list[Path]:
    """Find installed desktop executables through Windows App Paths registry keys."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    normalized_query = _normalize_app_name(query)
    candidates = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        base = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
        try:
            with winreg.OpenKey(hive, base) as root:
                count = winreg.QueryInfoKey(root)[0]
                for index in range(count):
                    try:
                        entry_name = winreg.EnumKey(root, index)
                        normalized_entry = _normalize_app_name(Path(entry_name).stem)
                        if not normalized_entry:
                            continue
                        if normalized_entry in normalized_query:
                            score = len(normalized_entry) * 100
                        elif normalized_query in normalized_entry:
                            score = len(normalized_query) * 50
                        elif len(normalized_query) >= 4 and _is_subsequence(normalized_query, normalized_entry):
                            score = len(normalized_query) * 10
                        else:
                            continue
                        with winreg.OpenKey(root, entry_name) as entry:
                            value, _ = winreg.QueryValueEx(entry, None)
                        executable = Path(str(value).strip().strip('"'))
                        if executable.is_file():
                            candidates.append((score, executable.resolve()))
                    except (OSError, ValueError):
                        continue
        except OSError:
            continue
    if not candidates:
        return []
    highest = max(score for score, _path in candidates)
    unique = {str(path).casefold(): path for score, path in candidates if score == highest}
    return sorted(unique.values(), key=lambda path: str(path).casefold())


def _shell_execute(target: str | Path, parameters: str = "", cwd: str | Path | None = None,
                   verb: str = "open") -> tuple[int | None, int | None]:
    """Launch a file or Start Menu shortcut through Windows ShellExecuteEx."""
    if os.name != "nt":
        raise OSError("通用桌面应用启动目前仅支持 Windows。")
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = verb
    info.lpFile = str(target)
    info.lpParameters = parameters or None
    info.lpDirectory = str(cwd) if cwd else None
    info.nShow = 1  # SW_SHOWNORMAL
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFO)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(target))
    pid = None
    handle = int(info.hProcess) if info.hProcess else None
    if handle:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel32.GetProcessId.restype = wintypes.DWORD
        pid = int(kernel32.GetProcessId(wintypes.HANDLE(handle))) or None
    return pid, handle


def _process_image_path(pid: int) -> str | None:
    """Return the image path for a Windows PID when query access is available."""
    if os.name != "nt" or not pid:
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _visible_window_title(pid: int | None = None, app_name: str = "") -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        titles = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        wanted = _normalize_app_name(app_name)
        process_path = _process_image_path(pid) if pid is not None else None
        shell_brokers = {"explorer.exe", "applicationframehost.exe", "startmenuexperiencehost.exe"}
        pid_is_app = bool(pid and (not process_path or Path(process_path).name.casefold() not in shell_brokers))

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            matches_pid = pid_is_app and process_id.value == pid
            matches_name = bool(wanted and wanted in _normalize_app_name(title))
            if title and (matches_pid or matches_name):
                titles.append(title)
                return False
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return titles[0] if titles else None
    except Exception:
        return None


def register_builtin_tools(registry: ToolRegistry, allow_dangerous: bool = False,
                           default_shell: str = "CMD", image_callback=None,
                           default_model: str | None = None, progress_callback=None,
                           web_search_enabled: bool = True):
    """注册内置工具：文件、目录、命令行。"""

    binary_suffixes = {
        ".doc", ".xls", ".ppt", ".docx", ".xlsx", ".xlsm", ".pptx", ".pptm",
        ".pdf", ".zip", ".7z", ".rar", ".exe", ".dll", ".sys", ".msi", ".bin",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".ico", ".mp3", ".wav",
        ".mp4", ".mov", ".avi", ".sqlite", ".db",
    }

    def confirm_mutation(message: str) -> bool:
        return bool(registry.confirm and registry.confirm(message))

    def confirm_path_access(path: Path, operation: str, overwrite: bool = False,
                            always: bool = False) -> bool:
        try:
            path.relative_to(registry.workspace)
            outside = False
        except ValueError:
            outside = True
        reasons = []
        if outside:
            reasons.append("路径位于工作空间外")
        if overwrite:
            reasons.append("目标已存在，将覆盖")
        if always and not reasons:
            reasons.append("该操作需要逐次确认")
        if reasons:
            why = "；".join(reasons)
            return confirm_mutation(f"Agent 请求{operation}：\n{path}\n\n{why}。是否继续？")
        return True

    # ---- 读文件 ----
    def read_file(args):
        p = registry._safe_path(args.get("path", ""))
        if not p.exists():
            return ToolResult(False, f"文件不存在：{p}")
        if not p.is_file():
            return ToolResult(False, f"不是文件：{p}")
        suffix = p.suffix.casefold()
        max_chars = max(1000, min(int(args.get("max_chars", 10000)), 30000))
        try:
            if suffix in {".docx", ".xlsx", ".xlsm", ".pptx", ".pptm"}:
                text = _extract_ooxml_text(p, max_chars)
            elif suffix == ".pdf":
                if p.stat().st_size > 100 * 1024 * 1024:
                    return ToolResult(False, "PDF 文件超过 100 MiB 安全读取上限；请先拆分或压缩。")
                from .rag import _read_document
                text = "\n\n".join(f"[第 {page} 页]\n{content}" for page, content in _read_document(p))
            elif suffix == ".zip":
                with zipfile.ZipFile(p) as archive:
                    entries = archive.infolist()
                text = "ZIP 清单（不会自动解压）：\n" + "\n".join(
                    f"{item.filename}\t{item.file_size} bytes" for item in entries[:500])
                if len(entries) > 500:
                    text += f"\n...[共 {len(entries)} 项，仅显示前 500 项]"
            elif suffix in {".doc", ".xls", ".ppt"}:
                return ToolResult(False, f"{suffix} 是旧版 Office 二进制格式，不能按文本解码。可用 inspect_file/copy_file/move_file/delete_file 管理；要读取正文请先用 Office 或 LibreOffice 另存为 DOCX/XLSX/PPTX。")
            elif suffix in binary_suffixes:
                return ToolResult(False, f"{suffix} 是二进制文件，不会把原始字节塞进模型上下文。可调用 inspect_file 查看大小/哈希/签名，或用 copy_file、move_file、delete_file 管理。")
            else:
                with p.open("rb") as source:
                    raw = source.read(8 * 1024 * 1024 + 1)
                source_was_truncated = len(raw) > 8 * 1024 * 1024
                if source_was_truncated:
                    raw = raw[:8 * 1024 * 1024]
                if _is_probably_binary(raw[:8192]):
                    return ToolResult(False, "检测到二进制内容，拒绝用有损编码伪装成文本。可调用 inspect_file 查看文件信息，或使用相应格式的解析/转换工具。")
                text = None
                for encoding in ("utf-8-sig", "utf-16", "gb18030"):
                    try:
                        text = raw.decode(encoding)
                        break
                    except UnicodeError:
                        continue
                if text is None:
                    return ToolResult(False, "无法无损识别文本编码；未用替换字符伪造内容。可用 inspect_file 查看文件属性。")
                if source_was_truncated:
                    text += "\n...[源文件超过 8 MiB，仅读取开头 8 MiB；可用专用解析器]"
            lines = text.splitlines()
            start_line = max(1, int(args.get("start_line", 1)))
            max_lines = max(1, min(int(args.get("max_lines", 400)), 2000))
            selected = lines[start_line - 1:start_line - 1 + max_lines]
            text = "\n".join(selected)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n...[按 max_chars 截断]"
            if start_line > 1 or len(selected) < len(lines):
                next_line = start_line + len(selected)
                text += f"\n...[第 {start_line} 行起；下一段 start_line={next_line}；共 {len(lines)} 行]"
        except Exception as exc:
            return ToolResult(False, f"读取失败：{type(exc).__name__}: {exc}")
        return ToolResult(True, f"文件 {registry.display_path(p)} 内容（只返回文本/解析结果，不返回原始二进制）：\n{text}")

    registry.register(
        "read_file", "读取工作空间内的文本文件",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对工作空间的路径或绝对路径"},
            "start_line": {"type": "integer", "description": "从第几行开始读，默认 1"},
            "max_lines": {"type": "integer", "description": "最多读多少行，默认 400，最大 2000"},
            "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 10000，最大 30000"},
        }, "required": ["path"]},
        read_file,
    )

    def document_to_markdown(args):
        from .rag import _read_document
        source = registry._safe_path(args.get("path", ""))
        if not source.is_file() or source.suffix.lower() not in {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".pptm"}:
            return ToolResult(False, "支持 PDF、DOCX、XLSX/XLSM、PPTX/PPTM；旧版 DOC/XLS/PPT 需先转换为 OOXML 格式。")
        if source.suffix.lower() in {".xlsx", ".xlsm", ".pptx", ".pptm"}:
            try:
                extracted = _extract_ooxml_text(source)
            except Exception as exc:
                return ToolResult(False, f"Office 文件解析失败：{type(exc).__name__}: {exc}")
            pages = [("正文", extracted)]
        else:
            try:
                pages = _read_document(source)
            except Exception as exc:
                return ToolResult(False, f"文档解析失败：{type(exc).__name__}: {exc}")
        output_arg = str(args.get("output_path", "")).strip()
        target = registry._safe_path(output_arg) if output_arg else registry._safe_path(
            str(source.with_name(source.stem + ".extracted.md")))
        try:
            target.relative_to(registry.workspace)
        except ValueError:
            return ToolResult(False, "Markdown 输出必须位于当前工作空间内。")
        if target.exists():
            if not registry.confirm or not registry.confirm(f"转换结果文件已存在，是否覆盖？\n{target}"):
                return ToolResult(False, f"输出文件已存在，未覆盖：{target}")
        parts = [f"# {source.stem}\n"]
        for heading, text in pages:
            if len(pages) > 1:
                parts.append(f"\n## 第 {heading} 页\n")
            parts.append(text.strip())
        content = "\n\n".join(part for part in parts if part)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(True, f"已提取 {len(pages)} 个页面/正文段落，写入 {registry.display_path(target)}。扫描版 PDF 需要 OCR，表格与版式不保证还原。")

    registry.register(
        "document_to_markdown", "将工作空间内的可选中文本 PDF 或 DOCX 提取为 Markdown 文件；扫描 PDF 需 OCR",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "PDF 或 DOCX 文件路径"},
            "output_path": {"type": "string", "description": "可选 Markdown 输出路径，必须在工作空间内"},
        }, "required": ["path"]}, document_to_markdown)

    # ---- 写文件 ----
    def write_file(args):
        p = registry._safe_path(args.get("path", ""))
        content = args.get("content", "")
        if not isinstance(content, str):
            return ToolResult(False, "write_file 只接收文本；二进制文件请使用 copy_file、download_file 或专用格式工具。")
        if "\x00" in content:
            return ToolResult(False, "文本中包含 NUL 字节，拒绝伪装写入二进制格式。")
        if p.suffix.casefold() in binary_suffixes:
            return ToolResult(False, f"{p.suffix} 是二进制/容器格式，不能用文本写入避免损坏。可用 read_file/inspect_file/verify_file 读取或检查，copy_file/move_file 管理；ZIP 用 create_archive/extract_archive。")
        if len(content.encode("utf-8")) > 1_048_576:
            return ToolResult(False, "文本写入上限为 1 MiB；请拆分内容或使用合适的文件生成器。")
        overwrite = p.exists()
        if overwrite and not p.is_file():
            return ToolResult(False, f"目标已存在但不是普通文件：{p}")
        if not confirm_path_access(p, "创建或覆盖文件", overwrite=overwrite):
            return ToolResult(False, f"创建/覆盖已取消：{registry.display_path(p)}")
        temporary = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, p)
            temporary = None
            written = p.read_bytes().decode("utf-8")
            if written != content:
                return ToolResult(False, f"写入后回读校验不一致：{registry.display_path(p)}")
            return ToolResult(True, f"已写入并回读校验：{registry.display_path(p)}（{len(content)} 字符，{p.stat().st_size} 字节）")
        except Exception as exc:
            return ToolResult(False, f"写入失败：{type(exc).__name__}: {exc}")
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    registry.register(
        "write_file", "创建文本文件并回读校验；拒绝以文本损坏 Office/图片/压缩包/可执行文件；覆盖或工作空间外写入需要确认",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对工作空间的路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        }, "required": ["path", "content"]},
        write_file,
    )

    def edit_file(args):
        p = registry._safe_path(args.get("path", ""))
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        if not p.is_file():
            return ToolResult(False, f"要修改的文件不存在：{p}")
        if p.suffix.casefold() in binary_suffixes:
            return ToolResult(False, f"{p.suffix} 是二进制/容器格式，不能按文本替换。使用对应 Office 编辑器或格式专用工具。")
        if not isinstance(old_text, str) or not old_text:
            return ToolResult(False, "old_text 必须是非空、完整且唯一的原文片段。")
        if not isinstance(new_text, str):
            return ToolResult(False, "new_text 必须是文本。")
        if p.stat().st_size > 5 * 1024 * 1024:
            return ToolResult(False, "文本修改上限为 5 MiB；请拆分修改。")
        raw = p.read_bytes()
        if _is_probably_binary(raw[:8192]):
            return ToolResult(False, "检测到二进制内容，拒绝文本替换。")
        source = None
        encoding_used = None
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            try:
                source = raw.decode(encoding)
                encoding_used = "utf-8" if encoding == "utf-8-sig" else encoding
                break
            except UnicodeError:
                continue
        if source is None:
            return ToolResult(False, "不能无损识别原文件编码；未做修改。")
        count = source.count(old_text)
        if count == 0:
            return ToolResult(False, "原文片段未找到，文件未修改。请先读取最新内容并提供准确片段。")
        replace_all = bool(args.get("replace_all", False))
        if count > 1 and not replace_all:
            return ToolResult(False, f"原文片段匹配 {count} 次；为避免误改，请提供更长且唯一的片段，或明确设置 replace_all=true。")
        updated = source.replace(old_text, new_text) if replace_all else source.replace(old_text, new_text, 1)
        if len(updated.encode("utf-8")) > 1_048_576:
            return ToolResult(False, "修改后文本超过 write_file 的 1 MiB 上限，未修改。")
        # write_file enforces overwrite/external-path confirmation and atomic write/readback.
        result = write_file({"path": str(p), "content": updated})
        if not result.ok:
            return result
        return ToolResult(True, f"已按 {encoding_used} 解码原文，精确替换 {count if replace_all else 1} 处，并回读核验：{registry.display_path(p)}")

    registry.register("edit_file", "精确替换文本文件中的唯一片段；不改 Office/压缩包/EXE 等二进制；覆盖前确认并原子写入、回读核验",
                      {"type": "object", "properties": {
                          "path": {"type": "string"},
                          "old_text": {"type": "string", "description": "要被替换的原文，必须精确匹配"},
                          "new_text": {"type": "string", "description": "替换后的文本"},
                          "replace_all": {"type": "boolean", "description": "多个匹配时是否全部替换，默认 false"},
                      }, "required": ["path", "old_text", "new_text"]}, edit_file)

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
        try:
            p.relative_to(registry.workspace)
            location = "工作空间内"
        except ValueError:
            location = "工作空间外"
        if not registry.confirm(f"Agent 请求永久删除{location}的单个文件：\n{rel}\n\n是否删除？"):
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

    # ---- 创建目录 ----
    def create_directory(args):
        p = registry._safe_path(args.get("path", ""))
        if p.exists():
            return ToolResult(p.is_dir(), f"目录已存在：{registry.display_path(p)}" if p.is_dir() else f"目标不是目录：{p}")
        if not confirm_path_access(p, "创建目录"):
            return ToolResult(False, f"创建目录已取消：{registry.display_path(p)}")
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult(p.is_dir(), f"已创建并核验目录：{registry.display_path(p)}" if p.is_dir() else f"创建目录失败：{p}")

    registry.register(
        "create_directory", "递归创建目录并确认目录实际存在",
        {"type": "object", "properties": {"path": {"type": "string", "description": "目录路径"}}, "required": ["path"]},
        create_directory)

    # ---- 文件元信息、复制与移动 ----
    def inspect_file(args):
        p = registry._safe_path(args.get("path", ""))
        if not p.is_file():
            return ToolResult(False, f"文件不存在或不是普通文件：{p}")
        try:
            stat_result = p.stat()
            digest = _sha256_file(p)
            with p.open("rb") as source:
                header = source.read(16)
            signatures = {
                b"MZ": "Windows PE/EXE/DLL", b"PK\x03\x04": "ZIP/OOXML 容器",
                b"%PDF-": "PDF", b"\x89PNG\r\n\x1a\n": "PNG 图片",
                b"\xff\xd8\xff": "JPEG 图片", b"GIF8": "GIF 图片",
                b"SQLite format 3\x00": "SQLite 数据库",
            }
            kind = next((label for signature, label in signatures.items() if header.startswith(signature)), "未识别/文本/其他二进制")
            modified = datetime.fromtimestamp(stat_result.st_mtime).astimezone().isoformat(timespec="seconds")
            return ToolResult(True, f"文件：{registry.display_path(p)}\n类型提示：{kind}\n扩展名：{p.suffix or '无'}\n大小：{stat_result.st_size} 字节\n修改时间：{modified}\nSHA-256：{digest}\n未读取或返回原始二进制内容。")
        except OSError as exc:
            return ToolResult(False, f"检查文件失败：{type(exc).__name__}: {exc}")

    registry.register("inspect_file", "安全检查任意文件的大小、时间、SHA-256 和文件签名，不读取二进制正文",
                      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, inspect_file)

    def copy_file(args):
        source = registry._safe_path(args.get("source", ""))
        destination = registry._safe_path(args.get("destination", ""))
        if not source.is_file():
            return ToolResult(False, f"源文件不存在或不是普通文件：{source}")
        if source == destination:
            return ToolResult(False, "源路径和目标路径相同，未复制。")
        if destination.exists() and not destination.is_file():
            return ToolResult(False, f"目标不是普通文件：{destination}")
        if not confirm_path_access(destination, "复制文件", overwrite=destination.exists()):
            return ToolResult(False, f"复制已取消：{registry.display_path(destination)}")
        try:
            _atomic_copy_file(source, destination)
            same = source.stat().st_size == destination.stat().st_size and _sha256_file(source) == _sha256_file(destination)
            if not same:
                return ToolResult(False, f"复制后校验失败：{registry.display_path(destination)}")
            return ToolResult(True, f"已复制并校验 SHA-256：{registry.display_path(source)} → {registry.display_path(destination)}（{destination.stat().st_size} 字节）")
        except OSError as exc:
            return ToolResult(False, f"复制失败：{type(exc).__name__}: {exc}")

    registry.register("copy_file", "复制任意单个文件（含二进制）；流式拷贝后校验长度和 SHA-256，覆盖和工作空间外写入需确认",
                      {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}, copy_file)

    def move_file(args):
        source = registry._safe_path(args.get("source", ""))
        destination = registry._safe_path(args.get("destination", ""))
        if not source.is_file():
            return ToolResult(False, f"源文件不存在或不是普通文件：{source}")
        if source == destination:
            return ToolResult(False, "源路径和目标路径相同，未移动。")
        if destination.exists() and not destination.is_file():
            return ToolResult(False, f"目标不是普通文件：{destination}")
        if not confirm_path_access(destination, f"移动文件 {source} → {destination}",
                                   overwrite=destination.exists(), always=True):
            return ToolResult(False, "目标路径访问未获确认，已取消移动。")
        try:
            source_digest = _sha256_file(source)
            _atomic_copy_file(source, destination)
            if _sha256_file(destination) != source_digest:
                return ToolResult(False, "移动目标校验失败；源文件保留。")
            source.unlink()
            if source.exists():
                return ToolResult(False, f"目标已校验，但源文件仍存在：{source}")
            return ToolResult(True, f"已移动并校验：{registry.display_path(source)} → {registry.display_path(destination)}")
        except OSError as exc:
            return ToolResult(False, f"移动失败：{type(exc).__name__}: {exc}；请检查源文件是否仍在原处。")

    registry.register("move_file", "移动任意单个文件（含二进制）；先复制并校验 SHA-256 再删除源文件，每次移动需确认",
                      {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}, move_file)

    def list_archive(args):
        archive_path = registry._safe_path(args.get("path", ""))
        if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
            return ToolResult(False, f"不是可读取的 ZIP 文件：{archive_path}")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                items = archive.infolist()
            lines = [f"{item.filename}\t{item.file_size} bytes" for item in items[:500]]
            if len(items) > 500:
                lines.append(f"...[总计 {len(items)} 项，仅显示前 500 项]")
            return ToolResult(True, f"ZIP 清单：{registry.display_path(archive_path)}\n" + "\n".join(lines))
        except (OSError, zipfile.BadZipFile) as exc:
            return ToolResult(False, f"ZIP 清单读取失败：{type(exc).__name__}: {exc}")

    registry.register("list_archive", "只列出 ZIP 文件内的路径和大小，不解压或执行文件",
                      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, list_archive)

    def create_archive(args):
        raw_paths = args.get("paths", [])
        if not isinstance(raw_paths, list) or not raw_paths:
            return ToolResult(False, "paths 必须是一个或多个文件/目录路径。")
        output_path = registry._safe_path(args.get("output_path", ""))
        if output_path.suffix.casefold() != ".zip":
            return ToolResult(False, "输出路径必须以 .zip 结尾。")
        if not confirm_path_access(output_path, "创建 ZIP 压缩包", overwrite=output_path.exists()):
            return ToolResult(False, "创建压缩包未获确认，已取消。")
        try:
            members: list[tuple[Path, str]] = []
            seen = set()
            total_bytes = 0
            for raw in raw_paths:
                source = registry._safe_path(str(raw))
                if not source.exists():
                    return ToolResult(False, f"压缩源不存在：{source}")
                if source.is_file():
                    candidates = [(source, source.name)]
                elif source.is_dir():
                    candidates = []
                    for current, dirnames, filenames in os.walk(source, followlinks=False):
                        dirnames[:] = [name for name in dirnames if not (Path(current) / name).is_symlink()]
                        for filename in filenames:
                            item = Path(current) / filename
                            if item.is_symlink():
                                continue
                            candidates.append((item, (Path(source.name) / item.relative_to(source)).as_posix()))
                else:
                    return ToolResult(False, f"不支持的压缩源：{source}")
                for item, arcname in candidates:
                    if output_path == item:
                        continue
                    if arcname in seen:
                        return ToolResult(False, f"压缩路径重名：{arcname}；请分批或重命名源。")
                    seen.add(arcname)
                    total_bytes += item.stat().st_size
                    members.append((item, arcname))
                    if len(members) > 5000 or total_bytes > 1024 * 1024 * 1024:
                        return ToolResult(False, "压缩任务超过上限（5000 个文件或 1 GiB 源数据）。")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=str(output_path.parent))
            os.close(fd)
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for item, arcname in members:
                        archive.write(item, arcname)
                with zipfile.ZipFile(temporary) as archive:
                    bad_member = archive.testzip()
                    count = len(archive.infolist())
                if bad_member:
                    return ToolResult(False, f"创建后 ZIP 校验失败，损坏成员：{bad_member}")
                os.replace(temporary, output_path)
                return ToolResult(True, f"已创建并校验 ZIP：{registry.display_path(output_path)}（{count} 项，源数据 {total_bytes} 字节）")
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except (OSError, zipfile.BadZipFile) as exc:
            return ToolResult(False, f"创建 ZIP 失败：{type(exc).__name__}: {exc}")

    registry.register("create_archive", "将指定文件/目录打包为 ZIP；不包含符号链接，最多 5000 项/1 GiB，并在写入后校验",
                      {"type": "object", "properties": {
                          "paths": {"type": "array", "items": {"type": "string"}, "description": "要打包的文件或目录路径列表"},
                          "output_path": {"type": "string", "description": "输出 ZIP 路径"},
                      }, "required": ["paths", "output_path"]}, create_archive)

    def extract_archive(args):
        archive_path = registry._safe_path(args.get("path", ""))
        if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
            return ToolResult(False, f"不是可读取的 ZIP 文件：{archive_path}")
        destination = registry._safe_path(args.get("destination", str(archive_path.with_suffix(""))))
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                total_size = sum(item.file_size for item in infos)
                if len(infos) > 5000 or total_size > 500 * 1024 * 1024:
                    return ToolResult(False, "拒绝解压：超过 5000 个成员或解压后 500 MiB 的安全上限。")
                for item in infos:
                    name = item.filename.replace("\\", "/")
                    posix_name = PurePosixPath(name)
                    win_name = PureWindowsPath(item.filename)
                    if not name or name.startswith("/") or posix_name.is_absolute() or win_name.is_absolute() or win_name.drive or ".." in posix_name.parts or "\x00" in name:
                        return ToolResult(False, f"拒绝解压：压缩包包含不安全路径 {item.filename!r}")
                    mode = (item.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        return ToolResult(False, f"拒绝解压符号链接成员：{item.filename}")
                    if item.file_size > max(100 * 1024 * 1024, item.compress_size * 1000):
                        return ToolResult(False, f"拒绝解压疑似压缩炸弹成员：{item.filename}")
                bad_member = archive.testzip()
                if bad_member:
                    return ToolResult(False, f"ZIP CRC 校验失败：{bad_member}")
                if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
                    return ToolResult(False, f"目标目录已存在且非空；为避免混合/覆盖文件，请指定一个新目录：{destination}")
                if not confirm_path_access(destination, "解压 ZIP", overwrite=destination.exists(), always=True):
                    return ToolResult(False, "解压需要确认，已取消。")
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".agent-extract-", dir=str(destination.parent)))
                try:
                    for item in infos:
                        relative = Path(*PurePosixPath(item.filename.replace("\\", "/")).parts)
                        target = (staging / relative).resolve()
                        target.relative_to(staging.resolve())
                        if item.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item, "r") as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                    if destination.exists():
                        destination.rmdir()
                    os.replace(staging, destination)
                    return ToolResult(True, f"已安全解压并完成 CRC 校验：{registry.display_path(destination)}（{len(infos)} 项，{total_size} 字节；不会执行文件）")
                finally:
                    if staging.exists():
                        shutil.rmtree(staging, ignore_errors=True)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            return ToolResult(False, f"解压失败：{type(exc).__name__}: {exc}")

    registry.register("extract_archive", "安全解压 ZIP 至新目录；检查路径穿越/符号链接/CRC，限制 5000 项和 500 MiB，需确认且绝不执行内容",
                      {"type": "object", "properties": {
                          "path": {"type": "string", "description": "ZIP 文件路径"},
                          "destination": {"type": "string", "description": "解压目录；省略时使用压缩包同名目录"},
                      }, "required": ["path"]}, extract_archive)

    def verify_file(args):
        p = registry._safe_path(args.get("path", ""))
        if not p.is_file():
            return ToolResult(False, f"文件不存在或不是普通文件：{p}")
        suffix = p.suffix.casefold()
        try:
            if suffix == ".py":
                source = p.read_text(encoding="utf-8-sig")
                compile(source, str(p), "exec")  # syntax check only; never execute
                return ToolResult(True, f"Python 语法校验通过：{registry.display_path(p)}（未执行代码）")
            if suffix == ".json":
                with p.open("r", encoding="utf-8-sig") as source:
                    json.load(source)
                return ToolResult(True, f"JSON 解析校验通过：{registry.display_path(p)}")
            if suffix == ".zip" or suffix in {".docx", ".xlsx", ".xlsm", ".pptx", ".pptm"}:
                with zipfile.ZipFile(p) as archive:
                    infos = archive.infolist()
                    if len(infos) > 5000 or sum(item.file_size for item in infos) > 500 * 1024 * 1024:
                        return ToolResult(False, "压缩容器超过校验上限（5000 项/500 MiB 展开大小）。")
                    bad_member = archive.testzip()
                    names = set(archive.namelist())
                if bad_member:
                    return ToolResult(False, f"ZIP CRC 校验失败：{bad_member}")
                required = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml",
                            ".xlsm": "xl/workbook.xml", ".pptx": "ppt/presentation.xml",
                            ".pptm": "ppt/presentation.xml"}
                expected = required.get(suffix)
                if expected and expected not in names:
                    return ToolResult(False, f"缺少 Office 必需成员：{expected}")
                return ToolResult(True, f"ZIP/Office 容器 CRC 校验通过：{registry.display_path(p)}（{len(infos)} 项）")
            if suffix == ".exe":
                with p.open("rb") as source:
                    if source.read(2) != b"MZ":
                        return ToolResult(False, "EXE 签名无效：缺少 MZ 头。未执行程序。")
                    source.seek(0x3C)
                    offset = int.from_bytes(source.read(4), "little")
                    source.seek(offset)
                    if source.read(4) != b"PE\0\0":
                        return ToolResult(False, "PE 签名无效。未执行程序。")
                return ToolResult(True, f"Windows PE 签名存在：{registry.display_path(p)}；未执行程序，也未验证发布者签名。")
            if suffix in {".doc", ".xls", ".ppt"}:
                with p.open("rb") as source:
                    signature = source.read(8)
                if signature[:4] != b"\xd0\xcf\x11\xe0":
                    return ToolResult(False, "旧版 Office OLE 文件签名无效。")
                return ToolResult(True, f"旧版 Office OLE 容器签名存在：{registry.display_path(p)}；未解析正文。")
            if suffix == ".pdf":
                with p.open("rb") as source:
                    valid = source.read(5) == b"%PDF-"
                return ToolResult(valid, "PDF 文件头校验通过。" if valid else "PDF 文件头无效。")
            if suffix in {".txt", ".md", ".bat", ".cmd", ".ps1", ".py", ".js", ".ts", ".c", ".cpp", ".json", ".html", ".css"}:
                p.read_text(encoding="utf-8-sig")
                return ToolResult(True, f"UTF-8 文本可完整读取：{registry.display_path(p)}")
            return ToolResult(True, f"文件存在且可读取：{registry.display_path(p)}（{p.stat().st_size} 字节）；当前没有该格式的内容校验器，未执行文件。")
        except (OSError, UnicodeError, SyntaxError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            return ToolResult(False, f"校验失败：{type(exc).__name__}: {exc}")

    registry.register("verify_file", "校验文件真实性/语法：Python 只编译不执行，JSON 解析，ZIP/OOXML CRC，EXE 只查 PE 头，不启动文件",
                      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, verify_file)

    def download_file(args):
        url = str(args.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ToolResult(False, "只允许 HTTPS 下载地址；不接受嵌入账号密码的 URL。")
        target = registry._safe_path(args.get("path", ""))
        if target.exists() and not target.is_file():
            return ToolResult(False, f"目标不是普通文件：{target}")
        if not confirm_path_access(target, f"从 {parsed.hostname} 下载文件", overwrite=target.exists(), always=True):
            return ToolResult(False, "下载未获确认，未连接远端或写入文件。")
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".download", dir=str(target.parent))
            total = 0
            digest = hashlib.sha256()
            request = urllib.request.Request(url, headers={"User-Agent": "LocalAgent/1.0"})
            with os.fdopen(fd, "wb") as output:
                with urllib.request.urlopen(request, timeout=30) as response:
                    final_url = urlparse(response.geturl())
                    if final_url.scheme.casefold() != "https":
                        return ToolResult(False, "下载重定向到了非 HTTPS 地址，已拒绝保存。")
                    length = response.headers.get("Content-Length")
                    if length and int(length) > 100 * 1024 * 1024:
                        return ToolResult(False, "下载文件超过 100 MiB 上限，已取消。")
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > 100 * 1024 * 1024:
                            return ToolResult(False, "下载超过 100 MiB 上限，已停止。")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary, target)
            temporary = None
            return ToolResult(True, f"下载完成并写入：{registry.display_path(target)}\n大小：{total} 字节\nSHA-256：{digest.hexdigest()}\n文件未自动打开或执行。")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return ToolResult(False, f"下载失败：{type(exc).__name__}: {exc}")
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    registry.register("download_file", "从 HTTPS 下载文件到指定路径；每次确认，最多 100 MiB，流式写入并计算 SHA-256，不会自动打开或运行下载内容",
                      {"type": "object", "properties": {
                          "url": {"type": "string", "description": "HTTPS 下载 URL"},
                          "path": {"type": "string", "description": "本地目标路径"},
                      }, "required": ["url", "path"]}, download_file)

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
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", "venv")]
            for fn in filenames:
                if fnmatch.fnmatch(fn, pattern):
                    full = Path(dirpath) / fn
                    hits.append(registry.display_path(full))
            if len(hits) >= 200:
                truncated = True
                break
        if truncated:
            hits.append("...[结果过多，已截断]")
        count_text = f"至少 {len(hits) - 1}" if truncated else str(len(hits))
        return ToolResult(True, f"搜索 '{pattern}' 找到 {count_text} 个文件：\n" + "\n".join(hits))

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
                    if f.is_symlink() or f.stat().st_size > 2 * 1024 * 1024:
                        continue
                    raw = f.read_bytes()
                    if _is_probably_binary(raw[:8192]):
                        continue
                    decoded = None
                    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
                        try:
                            decoded = raw.decode(encoding)
                            break
                        except UnicodeError:
                            continue
                    if decoded is None:
                        continue
                    for n, line in enumerate(decoded.splitlines(), 1):
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
        timeout = max(1, min(int(args.get("timeout", 120)), 600))
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
            code, output, used_shell = execute_shell_command(command, registry.workspace, shell, timeout=timeout)
            return ToolResult(code == 0,
                              f"Shell：{used_shell}\n工作目录：{registry.workspace}\n命令：{command}\n退出码：{code}\n输出：\n{output}")
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"命令超时（{timeout}秒）：{command}；命令是否已产生副作用未知，请检查状态后再重试。")

    registry.register(
        "run_command", "在工作空间目录后台执行 CMD/PowerShell 命令；GUI 可配置为自主执行或逐次确认。不是操作系统沙盒。",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "shell": {"type": "string", "enum": ["CMD", "PowerShell"], "description": "命令行类型；省略时使用当前默认值"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 120，最大 600"},
        }, "required": ["command"]},
        run_command,
    )

    def run_python_file(args):
        """Run one explicitly named Python script without shell string/path concatenation."""
        path_text = str(args.get("path", "")).strip()
        if not path_text:
            return ToolResult(False, "请提供要运行的 .py 文件路径；没有执行命令。")
        try:
            script = registry._safe_path(path_text)
        except Exception as exc:
            return ToolResult(False, f"Python 文件路径无效：{type(exc).__name__}: {exc}")
        if script.suffix.casefold() != ".py":
            return ToolResult(False, f"只运行 .py 文件，收到：{script.suffix or '无扩展名'}。")
        if not script.is_file():
            return ToolResult(False, f"Python 文件不存在，未运行：{script}")
        raw_args = args.get("args", [])
        if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
            return ToolResult(False, "args 必须是字符串列表。")
        if len(raw_args) > 64 or sum(len(item) for item in raw_args) > 8000:
            return ToolResult(False, "脚本参数过多，已拒绝运行。")
        timeout = max(1, min(int(args.get("timeout", 120)), 600))
        if not allow_dangerous:
            if not registry.confirm:
                return ToolResult(False, "运行 Python 脚本需要用户确认；没有确认窗口，已拒绝。")
            if not registry.confirm(
                    f"请求运行 Python 文件：\n{script}\n\n"
                    "脚本会按当前 Windows 用户权限运行，可能访问文件、网络或启动其他程序。是否允许？"):
                return ToolResult(False, "用户拒绝运行 Python 文件。")
        try:
            proc = subprocess.run([sys.executable, str(script), *raw_args], cwd=str(script.parent),
                                  capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                  if os.name == "nt" else 0)
            output = (proc.stdout or "") + (proc.stderr or "")
            if len(output) > 20000:
                output = output[:20000] + "\n...[输出截断]"
            return ToolResult(proc.returncode == 0,
                              f"Python：{sys.executable}\n文件：{script}\n工作目录：{script.parent}\n"
                              f"退出码：{proc.returncode}\n输出：\n{output or '（无标准输出）'}")
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"运行超时（{timeout}秒）：{script}；程序是否产生副作用未知，请检查文件和进程。")
        except OSError as exc:
            return ToolResult(False, f"启动 Python 失败：{type(exc).__name__}: {exc}\n文件：{script}")

    registry.register(
        "run_python_file", "用当前 Python 解释器直接运行一个存在的 .py 文件，不拼接 shell 命令；按当前用户权限运行（不是沙盒），根据 GUI 命令权限设置要求确认",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作空间内的 .py 相对路径或允许访问的绝对路径"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "可选脚本参数"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 120，最大 600"},
        }, "required": ["path"]}, run_python_file)

    # ---- 工作空间信息 ----
    def current_datetime(args):
        now = datetime.now().astimezone()
        return ToolResult(True, f"本机当前日期时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}（{now.isoformat()}）")

    registry.register(
        "current_datetime", "查询本机真实当前日期、时间和时区；用户询问今天、现在或系统日期时使用",
        {"type": "object", "properties": {}, "required": []}, current_datetime)

    def web_search(args):
        from .web_search import search_web
        query = str(args.get("query", "")).strip()
        try:
            return ToolResult(True, search_web(query))
        except Exception as exc:
            return ToolResult(False, f"联网搜索失败：{type(exc).__name__}: {exc}。请说明无法核实，不要把训练记忆当作最新搜索结果。")

    if web_search_enabled:
        registry.register(
            "web_search", "使用 Bing 实时搜索公开网页并返回标题、摘要、URL 和可用发布日期；适用于最新新闻、天气、进展等。摘要未必等于全文，回答必须链接实际返回的 URL",
            {"type": "object", "properties": {
                "query": {"type": "string", "description": "精确、简短的搜索关键词"},
            }, "required": ["query"]}, web_search)

    def network_location(args):
        import urllib.request
        request = urllib.request.Request("https://ip.guide/", headers={"User-Agent": "LocalAgent/1.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
        location = data.get("location") or {}
        network = data.get("network") or {}
        asn = network.get("autonomous_system") or {}
        if not data.get("ip"):
            return ToolResult(False, "IP 定位服务未返回公网 IP。")
        return ToolResult(True, f"公网出口 IP：{data['ip']}\n粗略归属地：{location.get('city') or '未知'}，{location.get('country') or '未知'}\n时区：{location.get('timezone') or '未知'}\n网络/ASN：{asn.get('organization') or asn.get('name') or '未知'}\n说明：这是公网出口 IP 的数据库估算，不是 GPS；VPN、代理、企业网关或托管网络会让位置显示在出口节点。")

    registry.register(
        "network_location", "仅在用户询问‘我在哪/本机公网 IP/电脑出口位置’时查询公网出口 IP 及其粗略城市归属；网络请求会把请求来源 IP 暴露给 ip.guide；不是 GPS 定位",
        {"type": "object", "properties": {}, "required": []}, network_location)

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
        name = str(args.get("name", "")).strip()
        if not name:
            return ToolResult(False, "请提供开始菜单中显示的软件名称，或它的可执行文件路径。")
        target = str(args.get("path", "")).strip()
        if target:
            try:
                target_path = registry._safe_path(target)
            except ValueError as exc:
                return ToolResult(False, str(exc))
            if not target_path.exists():
                return ToolResult(False, f"目标路径不存在：{target_path}")

        if os.name != "nt":
            executable = shutil.which(name)
            if not executable:
                return ToolResult(False, f"在 PATH 中找不到应用：{name}")
            try:
                command = [executable] + ([str(target_path)] if target else [])
                process = subprocess.Popen(command, cwd=str(registry.workspace),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, close_fds=True)
                return ToolResult(True, f"已向系统提交启动请求：{executable}，PID：{process.pid}。")
            except Exception as exc:
                return ToolResult(False, f"启动失败：{type(exc).__name__}: {exc}")

        shortcut_path = None
        if Path(name).is_file() and Path(name).suffix.casefold() in {".exe", ".lnk"}:
            shortcut_path = Path(name).resolve()
        if shortcut_path is None:
            shortcuts = _find_start_menu_shortcuts(name)
            if len(shortcuts) > 1:
                return ToolResult(False, "找到多个匹配的开始菜单快捷方式，请提供更具体的软件名称：\n" +
                                  "\n".join(str(item) for item in shortcuts[:8]))
            if shortcuts:
                shortcut_path = shortcuts[0]
        if shortcut_path is None:
            registered_apps = _find_registered_app_paths(name)
            if len(registered_apps) > 1:
                return ToolResult(False, "Windows 应用注册表中有多个匹配程序，请提供更具体名称或 .exe 路径：\n" +
                                  "\n".join(str(item) for item in registered_apps[:8]))
            if registered_apps:
                shortcut_path = registered_apps[0]
        if shortcut_path is None:
            executable = shutil.which(name if name.casefold().endswith(".exe") else name + ".exe")
            if executable:
                shortcut_path = Path(executable)
        if shortcut_path is None:
            path_matches = _find_path_executables(name)
            if len(path_matches) > 1:
                return ToolResult(False, "PATH 中有多个匹配的程序，请提供更具体的软件名称或完整路径：\n" +
                                  "\n".join(str(item) for item in path_matches[:8]))
            if path_matches:
                shortcut_path = path_matches[0]
        if shortcut_path is None:
            return ToolResult(False, f"未在开始菜单快捷方式或 PATH 中找到“{name}”。请确认软件已安装，或提供 .exe/.lnk 的完整路径。")

        parameters = subprocess.list2cmdline([str(target_path)]) if target else ""
        try:
            pid, process_handle = _shell_execute(shortcut_path, parameters, registry.workspace)
        except Exception as exc:
            return ToolResult(False, f"找到应用入口 {shortcut_path}，但 Windows 启动失败：{type(exc).__name__}: {exc}")
        try:
            deadline = time.time() + 12
            visible_title = None
            while time.time() < deadline:
                visible_title = _visible_window_title(pid, name)
                if visible_title:
                    break
                time.sleep(0.25)
            if visible_title:
                return ToolResult(True, f"已确认应用窗口可见：{visible_title}\n"
                                  f"开始菜单入口：{shortcut_path}\nPID：{pid or 'Windows Shell 已交给应用处理'}"
                                  + (f"\n打开路径：{target_path}" if target else ""))
            return ToolResult(False, f"Windows 已接受启动请求，但 12 秒内未发现与“{name}”匹配的可见窗口。\n"
                              f"入口：{shortcut_path}\nPID：{pid or '不可用'}。未确认应用已显示；请检查是否在后台启动或应用自身报错。")
        finally:
            if process_handle:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(process_handle))

    def open_file(args):
        if os.name != "nt":
            return ToolResult(False, "当前通用文件关联打开工具仅支持 Windows。")
        raw_path = str(args.get("path", "")).strip()
        if not raw_path:
            return ToolResult(False, "请提供要打开的文件路径。")
        try:
            target_path = registry._safe_path(raw_path)
        except ValueError as exc:
            return ToolResult(False, str(exc))
        if not target_path.is_file():
            return ToolResult(False, f"文件不存在或不是普通文件：{target_path}")
        mode = str(args.get("mode", "auto")).casefold()
        safe_edit_extensions = {
            ".py", ".bat", ".cmd", ".ps1", ".js", ".mjs", ".cjs", ".ts", ".c", ".cpp",
            ".h", ".hpp", ".java", ".json", ".html", ".css", ".sh", ".psm1", ".go", ".rs",
        }
        if mode == "edit" or (mode == "auto" and target_path.suffix.casefold() in safe_edit_extensions):
            verb = "edit"
        else:
            verb = "open"
        try:
            pid, handle = _shell_execute(target_path, cwd=target_path.parent, verb=verb)
        except Exception as exc:
            return ToolResult(False, f"Windows 无法通过关联程序打开文件：{type(exc).__name__}: {exc}")
        try:
            deadline = time.time() + 12
            title = None
            while time.time() < deadline:
                title = _visible_window_title(pid, target_path.stem)
                if title:
                    break
                time.sleep(0.25)
            if title:
                return ToolResult(True, f"已确认文件在可见窗口中打开：{target_path}\n窗口：{title}\nPID：{pid or '由现有应用进程处理'}")
            return ToolResult(False, f"Windows 已接受打开请求，但 12 秒内未确认文件窗口：{target_path}\nPID：{pid or '不可用'}。未报告为已打开。")
        finally:
            if handle:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))

    registry.register(
        "open_file", "通过 Windows 文件关联打开工作空间内的文件；代码和脚本默认以 edit 动词交给编辑器，不会执行 BAT/PS1 等脚本；可显式指定 mode=open。只有检测到可见窗口才报告成功。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作空间内的文件路径"},
            "mode": {"type": "string", "enum": ["auto", "edit", "open"], "description": "auto 根据文件类型安全选择；edit 交给编辑器；open 使用 Windows 默认动作"},
        }, "required": ["path"]}, open_file)

    registry.register(
        "open_application", "通过 Windows 开始菜单快捷方式、App Paths 注册表或 PATH 查找并启动已安装桌面应用；启动后检查可见窗口并返回入口/PID。name 使用应用显示名称；path 可选为要交给应用打开的工作空间文件/目录。",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "开始菜单显示的软件名称，或 .exe/.lnk 完整路径"},
            "path": {"type": "string", "description": "可选：工作空间内的文件或文件夹"},
        }, "required": ["name"]}, open_application)

    def open_vscode(args):
        return open_application({"name": "vscode", **args})

    registry.register(
        "open_vscode", "兼容旧调用名：通过通用应用发现与窗口验证机制打开 Visual Studio Code；默认打开当前工作空间。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "可选：工作空间内的文件或文件夹"},
        }, "required": []}, open_vscode)

    registry.register(
        "execute_command", "兼容常见模型命名的命令工具别名；在工作空间执行命令，每条都需用户确认",
        {"type": "object", "properties": {
            "command": {"type": "string"},
            "shell": {"type": "string", "enum": ["CMD", "PowerShell"]},
            "timeout": {"type": "integer", "description": "超时秒数，默认 120，最大 600"},
        }, "required": ["command"]}, run_command)
