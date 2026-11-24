"""底层系统控制工具 —— 让 Agent 能观察和控制本机软件/硬件的底层状态。

读操作（进程、硬件、串口、窗口、服务）默认可用；
写操作（结束进程、开关服务、读写串口）走 confirm 回调，用户可见可控。

注意：这是当前 Windows 用户权限下的真实系统访问，精心设计了只读/写入分离，
不是 OS 沙盒。管理员级的修改仍需 GUI 以管理员身份启动 + UAC 授权。
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .tools import ToolResult

IS_WINDOWS = os.name == "nt"


def _run_ps(script: str, timeout: int = 30):
    """通过 PowerShell 执行只读/受控脚本。"""
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def _system_overview():
    data = {"platform": platform.platform(), "system": platform.system(),
            "release": platform.release(), "machine": platform.machine(),
            "processor": platform.processor() or "未知"}
    if IS_WINDOWS:
        try:
            ver = sys.getwindowsversion()
            data["windows"] = f"build {ver.build}, {ver.major}.{ver.minor}"
        except Exception:
            pass
        try:
            import psutil
            vm = psutil.virtual_memory()
            data["memory"] = f"总 {vm.total // (1024**3)}GB，可用 {vm.available // (1024**2)}MB ({vm.percent}%)"
            data["cpu"] = f"{psutil.cpu_count(logical=False)} 核 / {psutil.cpu_count()} 线程，负载 {psutil.cpu_percent(interval=0.3)}%"
            disks = []
            for part in psutil.disk_partitions():
                if "cdrom" in part.opts:
                    continue
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append(f"{part.device} 空闲 {usage.free // (1024**3)}GB/{usage.total // (1024**3)}GB")
                except Exception:
                    pass
            data["disks"] = "；".join(disks[:6])
        except ImportError:
            data["psutil"] = "未安装，内存/磁盘详情不可用"
    return data


def _list_processes(name_filter: str = "", limit: int = 60):
    try:
        import psutil
    except ImportError:
        proc = _run_ps("Get-Process | Select-Object -First 60 Name,Id,CPU,WorkingSet64 | Format-Table -AutoSize | Out-String -Width 200")
        return proc.stdout[:6000] or proc.stderr[:2000] or "无进程信息"
    rows = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = p.info
            name = info.get("name") or ""
            if name_filter and name_filter.casefold() not in name.casefold():
                continue
            mem = (info.get("memory_info").rss // (1024**2)) if info.get("memory_info") else 0
            rows.append((name, info.get("pid"), mem))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda item: item[2], reverse=True)
    lines = [f"{name}  PID={pid}  内存={mem}MB" for name, pid, mem in rows[:limit]]
    return f"匹配 {len(rows)} 个进程，显示前 {min(limit, len(rows))} 个：\n" + "\n".join(lines)


def _list_serial_ports():
    if not IS_WINDOWS:
        result = _run_ps("[System.IO.Ports.SerialPort]::GetPortNames() 2>$null; if(-not $?) { Get-ChildItem /dev/tty* 2>$null }")
        return result.stdout[:2000]
    result = _run_ps("[System.IO.Ports.SerialPort]::GetPortNames() | ForEach-Object { $_ }")
    ports = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if ports:
        return "检测到串口：" + "，".join(ports) + "\n说明：如果要读写串口数据，用 create_tool 生成一个 import serial 的工具（pySerial 可用 install_package 安装）。"
    # 回退到注册表枚举
    reg = _run_ps("Get-ItemProperty 'HKLM:\\HARDWARE\\DEVICEMAP\\SERIALCOMM' -ErrorAction SilentlyContinue | Format-List | Out-String")
    return ("未通过 .NET 枚举到串口。注册表信息：\n" + reg.stdout[:1500]) if reg.stdout.strip() else "未检测到串口设备。"


def register_system_control_tools(registry, confirm=None):
    def system_status(args):
        data = _system_overview()
        return ToolResult(True, "本机系统状态：\n" + "\n".join(f"{k}: {v}" for k, v in data.items()))

    def list_processes(args):
        name = str(args.get("name", "")).strip()
        limit = max(1, min(int(args.get("limit", 60) or 60), 200))
        try:
            return ToolResult(True, _list_processes(name, limit))
        except Exception as exc:
            return ToolResult(False, f"列出进程失败：{exc}")

    def terminate_process(args):
        pid = args.get("pid")
        name = str(args.get("name", "")).strip()
        if not pid and not name:
            return ToolResult(False, "请提供 pid 或进程名 name。")
        target = f"PID {pid}" if pid else f"进程名 {name}"
        if confirm and not confirm(f"Agent 请求结束进程 {target}。正在运行的程序会立即被强制关闭、未保存数据可能丢失。是否继续？"):
            return ToolResult(False, "用户拒绝结束进程。")
        try:
            import psutil
            victims = []
            if pid:
                try:
                    victims.append(psutil.Process(int(pid)))
                except Exception:
                    return ToolResult(False, f"找不到 PID {pid}")
            else:
                for p in psutil.process_iter(["name"]):
                    if p.info.get("name", "").casefold() == name.casefold():
                        victims.append(p)
            if not victims:
                return ToolResult(False, f"找不到进程 {target}")
            done = []
            for p in victims:
                p.terminate()
                done.append(f"{p.name()} (PID {p.pid})")
            return ToolResult(True, f"已发送终止信号：{', '.join(done)}。请复查进程是否真的结束。")
        except Exception as exc:
            return ToolResult(False, f"结束进程失败：{exc}")

    def query_windows(args):
        """枚举当前用户桌面上可见的顶层窗口。"""
        if not IS_WINDOWS:
            return ToolResult(False, "窗口枚举仅支持 Windows。")
        titles = []

        def collect(hwnd, _):
            if ctypes.windll.user32.IsWindowVisible(hwnd):
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                    if buf.value.strip():
                        titles.append(buf.value.strip())
            return True

        try:
            CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            ctypes.windll.user32.EnumWindows(CB(collect), 0)
            return ToolResult(True, f"检测到 {len(titles)} 个可见窗口：\n" + "\n".join(f"- {t}" for t in titles[:80]))
        except Exception as exc:
            return ToolResult(False, f"窗口枚举失败：{exc}")

    def service_control(args):
        name = str(args.get("name", "")).strip()
        action = str(args.get("action", "query")).casefold()
        if not name or not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
            return ToolResult(False, "请提供合法的服务名 name。")
        if action in {"start", "stop", "restart"}:
            if confirm and not confirm(f"Agent 请求 {action} 系统服务「{name}」。是否允许？"):
                return ToolResult(False, "用户拒绝操作服务。")
            script = {"start": f"Start-Service '{name}'", "stop": f"Stop-Service '{name}' -Force",
                      "restart": f"Restart-Service '{name}' -Force"}[action]
        else:
            script = f"Get-Service -Name '{name}' -ErrorAction SilentlyContinue | Format-List Name,Status,StartType | Out-String"
        try:
            proc = _run_ps(script)
            output = (proc.stdout or proc.stderr or "").strip()[:3000]
            return ToolResult(bool(output), output or "服务无响应或名称不存在")
        except Exception as exc:
            return ToolResult(False, f"服务操作失败：{exc}")

    def list_services(args):
        name = str(args.get("name", "")).strip()
        script = ("Get-Service" + (f" -Name '*{name}*'" if name else "") +
                  " | Select-Object -First 80 Name,Status,StartType,DisplayName | Format-Table -AutoSize | Out-String -Width 200")
        try:
            proc = _run_ps(script)
            return ToolResult(True, (proc.stdout or proc.stderr or "")[:6000])
        except Exception as exc:
            return ToolResult(False, f"列出服务失败：{exc}")

    def serial_ports(args):
        try:
            return ToolResult(True, _list_serial_ports())
        except Exception as exc:
            return ToolResult(False, f"串口枚举失败：{exc}")

    def env_vars(args):
        keyword = str(args.get("keyword", "")).strip().casefold()
        items = []
        for key, value in os.environ.items():
            if keyword and keyword not in key.casefold():
                continue
            # 绝不返回任何凭据类环境变量的值
            if any(secret in key.casefold() for secret in ("password", "passwd", "token", "secret", "key", "credential")):
                value = "*** 已隐藏（可能是凭据）***"
            items.append(f"{key}={value[:300]}")
        return ToolResult(True, f"环境变量（{len(items)} 项）：\n" + "\n".join(sorted(items)[:120]))

    def registry_read(args):
        path = str(args.get("path", "")).strip()
        if not path or not (path.startswith("HKLM:") or path.startswith("HKCU:")):
            return ToolResult(False, "请提供 HKLM:\\ 或 HKCU:\\ 开头的注册表路径。")
        script = f"Get-ItemProperty -Path '{path}' -ErrorAction SilentlyContinue | Format-List | Out-String -Width 200"
        try:
            proc = _run_ps(script)
            output = (proc.stdout or proc.stderr or "").strip()[:4000]
            return ToolResult(bool(output), output or "注册表路径不存在或无权限")
        except Exception as exc:
            return ToolResult(False, f"读取注册表失败：{exc}")

    registry.register(
        "system_status", "查询本机真实硬件与系统状态：CPU、内存、磁盘空闲、Windows 版本、平台架构。用于了解当前机器能力",
        {"type": "object", "properties": {}, "required": []}, system_status, source="system")
    registry.register(
        "list_processes", "列出正在运行的进程及 PID、内存占用，可按进程名筛选。只读，不做任何修改",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "可选：按进程名筛选"},
            "limit": {"type": "integer", "description": "最多显示条数，默认 60"},
        }, "required": []}, list_processes, source="system")
    registry.register(
        "terminate_process", "按 PID 或进程名结束一个进程。会请求用户确认；强制关闭可能丢失未保存数据",
        {"type": "object", "properties": {
            "pid": {"type": "integer", "description": "进程 PID"},
            "name": {"type": "string", "description": "进程名，如 notepad.exe"},
        }, "required": []}, terminate_process, source="system")
    registry.register(
        "list_windows", "枚举当前桌面可见的窗口标题，用于确认软件是否真的打开了窗口。只读",
        {"type": "object", "properties": {}, "required": []}, query_windows, source="system")
    registry.register(
        "list_services", "查询 Windows 服务列表及其运行状态，可按名称筛选。只读",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "可选：按服务名筛选"},
        }, "required": []}, list_services, source="system")
    registry.register(
        "service_control", "查询或启动/停止/重启一个 Windows 服务。写操作会请求用户确认",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "服务名"},
            "action": {"type": "string", "description": "query/start/stop/restart，默认 query"},
        }, "required": ["name"]}, service_control, source="system")
    registry.register(
        "serial_ports", "枚举本机串口（COM 口）设备。需要读写串口数据时，用 create_tool 生成一个 import serial 的工具",
        {"type": "object", "properties": {}, "required": []}, serial_ports, source="system")
    registry.register(
        "env_vars", "查看环境变量（可按关键词筛选）。所有疑似凭据的变量值会被隐藏，绝不泄露密码/令牌",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "可选：筛选变量名"},
        }, "required": []}, env_vars, source="system")
    registry.register(
        "registry_read", "读取一个注册表键的值（仅 HKLM:/HKCU:，只读）。用于查看软件和硬件配置",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "如 HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"},
        }, "required": ["path"]}, registry_read, source="system")
