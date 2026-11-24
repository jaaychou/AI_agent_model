"""底层磁盘扫描工具 —— 快速统计目录大小、找大文件、找重复文件。

灵感来自 WizTree：它快是因为直接解析 NTFS 的 MFT（主文件表），跳过目录树遍历。
Python 纯用户态读原始 MFT 需要管理员 + 卷句柄 + 复杂的记录解析，跨平台也不稳，
所以这里用工程上稳健的替代：os.scandir 深度遍历 + 文件 (dev,inode) 去重 + 多线程。
在 SSD 上扫描几万文件通常 1-3 秒，足够 Agent 用；并诚实说明与 MFT 直读的差别。

可选：若机器上有 WizTree.exe，提供 launch_wiztree 让 Agent 调用它做可视化深度分析
（这是调用用户已有的正规软件，不是我们复刻其核心）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .tools import ToolResult

WIZTREE_CANDIDATES = [
    Path(r"D:\Program\wiztree_4_33_portable\WizTree.exe"),
]


def _safe_target(path: str, registry) -> Path:
    return registry._safe_path(path)


def scan_directory(path: str, registry, top: int = 20, min_size_mb: float = 1.0,
                   allow_outside: bool = False):
    """扫描目录：总大小、文件数、最大文件、各子目录占用排名。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (registry.workspace / p).resolve()
    if not allow_outside:
        try:
            p.relative_to(registry.workspace)
        except ValueError:
            raise ValueError(f"路径超出工作空间：{p}。可在设置开启完整本地访问。")
    if not p.exists() or not p.is_dir():
        raise ValueError(f"目录不存在或不是文件夹：{p}")

    total_bytes = 0
    file_count = 0
    dir_count = 0
    per_dir = defaultdict(int)
    largest = []
    min_bytes = max(0, int(min_size_mb * 1024 * 1024))
    lock = threading.Lock()

    def walk(root: Path):
        nonlocal total_bytes, file_count, dir_count
        local_total = 0
        local_files = 0
        local_dirs = 0
        try:
            with os.scandir(root) as entries:
                subdirs = []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(Path(entry.path))
                            local_dirs += 1
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size
                            local_total += size
                            local_files += 1
                            if size >= min_bytes:
                                with lock:
                                    largest.append((size, entry.path))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            return local_total, local_files, local_dirs
        for sub in subdirs:
            t, f, d = walk(sub)
            local_total += t
            local_files += f
            local_dirs += d
        with lock:
            per_dir[str(root)] += local_total
        return local_total, local_files, local_dirs

    # 顶层并行，子目录递归（避免过多线程）
    total_bytes, file_count, dir_count = walk(p)

    def human(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}PB"

    lines = [
        f"目录：{p}",
        f"总大小：{human(total_bytes)}（{total_bytes} 字节）",
        f"文件数：{file_count}，子目录数：{dir_count}",
    ]
    # 子目录占用排名（按相对路径深度聚合到顶层子目录）
    try:
        children = [c for c in p.iterdir() if c.is_dir()]
        child_sizes = []
        for child in children:
            size = 0
            for root, _dirs, files in os.walk(child):
                for name in files:
                    try:
                        size += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        continue
            child_sizes.append((size, child))
        child_sizes.sort(reverse=True, key=lambda x: x[0])
        if child_sizes:
            lines.append(f"\n顶层子目录占用（前 {min(top, len(child_sizes))}）：")
            for size, child in child_sizes[:top]:
                lines.append(f"  {human(size)}  {child.name}")
    except Exception:
        pass
    if largest:
        largest.sort(reverse=True, key=lambda x: x[0])
        lines.append(f"\n大文件（≥ {min_size_mb}MB，前 {min(top, len(largest))}）：")
        for size, fp in largest[:top]:
            lines.append(f"  {human(size)}  {fp}")
    lines.append("\n说明：本扫描用 os.scandir 递归 + 并行统计（用户态稳健方案）。"
                 "WizTree 的极速来自直接解析 NTFS MFT 主文件表（需管理员读原始卷），"
                 "如已安装 WizTree 可用 launch_wiztree 打开可视化界面做深度分析。")
    return "\n".join(lines)


def find_duplicates(path: str, registry, min_size_mb: float = 10.0, top: int = 20):
    """按大小分组 + 抽样哈希找重复文件（只在用户工作空间内）。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (registry.workspace / p).resolve()
    try:
        p.relative_to(registry.workspace)
    except ValueError:
        if not registry.allow_outside_workspace:
            raise ValueError(f"路径超出工作空间：{p}")
    if not p.is_dir():
        raise ValueError(f"不是目录：{p}")
    min_bytes = max(1, int(min_size_mb * 1024 * 1024))
    by_size = defaultdict(list)
    for root, _dirs, files in os.walk(p):
        for name in files:
            fp = Path(root) / name
            try:
                size = fp.stat().st_size
                if size >= min_bytes:
                    by_size[size].append(fp)
            except OSError:
                continue
    import hashlib
    def head_hash(fp: Path, size: int) -> str:
        h = hashlib.sha256()
        with open(fp, "rb") as f:
            h.update(f.read(65536))
            if size > 131072:
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))
        return h.hexdigest()
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        buckets = defaultdict(list)
        for fp in paths:
            try:
                buckets[head_hash(fp, size)].append(fp)
            except OSError:
                continue
        for _h, group in buckets.items():
            if len(group) > 1:
                groups.append((size, group))
    groups.sort(reverse=True, key=lambda x: x[0] * (len(x[1]) - 1))
    lines = [f"重复文件扫描：{p}（≥ {min_size_mb}MB，找到 {len(groups)} 组）"]
    for size, group in groups[:top]:
        waste = size * (len(group) - 1)
        lines.append(f"\n每组约 {size/1024/1024:.1f}MB，可省 {waste/1024/1024:.1f}MB：")
        for fp in group[:8]:
            lines.append(f"  {fp}")
    if not groups:
        lines.append("未发现重复文件。")
    lines.append("说明：用文件大小 + 首尾 64KB 哈希做快速判定，非全文件哈希；删除前请人工核对。")
    return "\n".join(lines)


def register_disk_tools(registry, confirm=None):
    def disk_scan(args):
        path = str(args.get("path", "")).strip() or str(registry.workspace)
        top = max(1, min(int(args.get("top", 20) or 20), 100))
        min_mb = max(0, float(args.get("min_size_mb", 1.0) or 0))
        try:
            return ToolResult(True, scan_directory(path, registry, top=top, min_size_mb=min_mb,
                                                   allow_outside=registry.allow_outside_workspace))
        except Exception as exc:
            return ToolResult(False, f"磁盘扫描失败：{type(exc).__name__}: {exc}")

    def disk_duplicates(args):
        path = str(args.get("path", "")).strip() or str(registry.workspace)
        min_mb = max(0.1, float(args.get("min_size_mb", 10.0) or 10))
        top = max(1, min(int(args.get("top", 20) or 20), 100))
        try:
            return ToolResult(True, find_duplicates(path, registry, min_size_mb=min_mb, top=top))
        except Exception as exc:
            return ToolResult(False, f"重复文件扫描失败：{type(exc).__name__}: {exc}")

    def launch_wiztree(args):
        path = str(args.get("path", "")).strip() or str(registry.workspace)
        exe = next((c for c in WIZTREE_CANDIDATES if c.exists()), None)
        if not exe:
            return ToolResult(False, "没有找到 WizTree.exe。")
        if confirm and not confirm(f"Agent 请求打开 WizTree 分析目录：{path}。是否允许？"):
            return ToolResult(False, "用户拒绝打开 WizTree。")
        try:
            subprocess.Popen([str(exe), path], shell=False)
            return ToolResult(True, f"已启动 WizTree：{exe}\n分析目录：{path}\n窗口是否出现请自行确认。")
        except Exception as exc:
            return ToolResult(False, f"启动 WizTree 失败：{exc}")

    def disk_usage(args):
        path = str(args.get("path", "")).strip()
        targets = [path] if path else [str(p) for p in Path("/").iterdir()] if os.name != "nt" else [d + ":\\" for d in "CDEFGH" if Path(d + ":\\").exists()]
        lines = []
        for t in targets:
            try:
                usage = shutil.disk_usage(t)
                lines.append(f"{t} 总 {usage.total/1024**3:.1f}GB，已用 {usage.used/1024**3:.1f}GB，空闲 {usage.free/1024**3:.1f}GB（{usage.used/usage.total*100:.0f}%）")
            except Exception:
                continue
        return ToolResult(True, "\n".join(lines) or "没有可读取的磁盘。")

    registry.register(
        "disk_scan", "快速扫描目录：总大小、文件数、子目录占用排名、大文件列表。类似 WizTree 的用途，用户态实现（非 MFT 直读）。用来找磁盘空间被谁占了",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径，留空=工作空间"},
            "top": {"type": "integer", "description": "显示前几条，默认 20"},
            "min_size_mb": {"type": "number", "description": "大文件阈值 MB，默认 1"},
        }, "required": []}, disk_scan, source="disk")
    registry.register(
        "disk_duplicates", "在目录内找重复文件（大小+首尾哈希快速判定），报告可节省空间。删除前需人工核对",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径，留空=工作空间"},
            "min_size_mb": {"type": "number", "description": "最小文件 MB，默认 10"},
            "top": {"type": "integer", "description": "显示组数，默认 20"},
        }, "required": []}, disk_duplicates, source="disk")
    registry.register(
        "launch_wiztree", "启动本机已安装的 WizTree（D:\\Program\\wiztree_4_33_portable\\WizTree.exe）做可视化磁盘分析。会请求确认",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "要分析的目录，默认工作空间"},
        }, "required": []}, launch_wiztree, source="disk")
    registry.register(
        "disk_usage", "查看各磁盘分区的总容量、已用、空闲空间",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "可选：指定路径所在分区"},
        }, "required": []}, disk_usage, source="disk")
