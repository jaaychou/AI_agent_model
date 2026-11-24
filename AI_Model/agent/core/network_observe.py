"""底层网络/无线可观测工具 —— 只读系统已暴露的状态信息。

严格边界（重要，勿删）：
- 只读取 Windows / psutil 已经暴露的聚合状态：WiFi 连接信息、蓝牙设备列表、
  网卡收发字节计数、进程网络连接、DNS 缓存。这些是普通用户/管理员都能合法查看的。
- 不做：原始 802.11 帧监听、蓝牙嗅探、网络抓包（需 Npcap 驱动、可读明文流量）、
  任何凭据读取或中间人。这类能力既违反本机 Agent 的安全边界，也通常是违法的。
- "实时监测"的实现方式：对聚合计数器按秒采样求速率（如 KB/s），不是抓包。

所有命令用 subprocess 数组参数调用，不经过 CMD 字符串拼接。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .tools import ToolResult

IS_WINDOWS = os.name == "nt"


def _run(args: list[str], timeout: int = 15) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        return (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    except Exception as exc:
        return f"命令执行失败：{type(exc).__name__}: {exc}"


def _wifi_status() -> str:
    if not IS_WINDOWS:
        return _run(["iwconfig"], timeout=10)
    return _run(["netsh", "wlan", "show", "interfaces"], timeout=15)


def _wifi_profiles() -> str:
    if not IS_WINDOWS:
        return "仅支持 Windows。"
    return _run(["netsh", "wlan", "show", "profiles"], timeout=15)


def _bluetooth_devices() -> str:
    """通过 PowerShell 只读查询蓝牙设备（Get-PnpDevice 蓝牙类）。"""
    if not IS_WINDOWS:
        return _run(["bluetoothctl", "devices"], timeout=10)
    ps = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
          "Select-Object FriendlyName,Status,InstanceId | Format-Table -AutoSize | Out-String -Width 200")
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=25)


def _net_counters() -> dict:
    import psutil
    return psutil.net_io_counters(pernic=True) or {}


def register_network_observe_tools(registry, confirm=None):
    def wifi_status(args):
        text = _wifi_status()
        if not text.strip() or "error" in text.casefold()[:80]:
            return ToolResult(False, "未获取到 WiFi 状态：可能没有无线网卡或未连接。")
        # 精简输出：只保留关键行，避免整页无关信息
        keys = ("名称", "SSID", "BSSID", "状态", "无线电类型", "频带", "通道",
                "接收速率", "传输速率", "信号", "说明", "连接模式", "网络类型",
                "State", "Signal", "Channel", "Band", "Radio", "Receive", "Transmit")
        lines = [line.rstrip() for line in text.splitlines()
                 if ":" in line and any(k in line for k in keys)]
        return ToolResult(True, "WiFi 当前状态（系统只读信息）：\n" + "\n".join(lines[:30]) if lines else text[:2000])

    def wifi_profiles(args):
        text = _wifi_profiles()
        lines = [line.rstrip() for line in text.splitlines() if "所有用户配置文件" in line or "All User Profile" in line]
        return ToolResult(True, "已保存的 WiFi 配置文件名（不读取密码）：\n" + "\n".join(lines[:40]) if lines else text[:2000])

    def bluetooth_devices(args):
        text = _bluetooth_devices()
        if not text.strip():
            return ToolResult(True, "未发现蓝牙设备或蓝牙未开启。")
        return ToolResult(True, "蓝牙设备（系统只读信息，PnP 枚举）：\n" + text[:4000])

    def net_traffic(args):
        """按秒采样网卡计数器，给出实时收发速率。只读聚合计数，不抓包。"""
        seconds = max(1, min(int(args.get("seconds", 2) or 2), 10))
        try:
            before = _net_counters()
        except Exception as exc:
            return ToolResult(False, f"读取网卡计数失败：{exc}")
        time.sleep(seconds)
        try:
            after = _net_counters()
        except Exception as exc:
            return ToolResult(False, f"读取网卡计数失败：{exc}")
        lines = [f"网卡实时速率（{seconds} 秒采样，聚合计数，非抓包）："]
        found = False
        for name, a in after.items():
            b = before.get(name)
            if not b:
                continue
            up = max(0, a.bytes_sent - b.bytes_sent) / seconds / 1024
            down = max(0, a.bytes_recv - b.bytes_recv) / seconds / 1024
            if up < 0.1 and down < 0.1:
                continue
            found = True
            lines.append(f"  {name}: ↓{down:.1f} KB/s  ↑{up:.1f} KB/s")
        if not found:
            lines.append("  采样期内没有明显流量。")
        lines.append("说明：这是网卡层面的收发字节速率，不能看到具体流量内容；抓包需额外驱动且涉及隐私，本 Agent 不做。")
        return ToolResult(True, "\n".join(lines))

    def net_connections(args):
        import psutil
        lines = ["当前网络连接（只读枚举）："]
        count = 0
        for conn in psutil.net_connections(kind="inet"):
            if count >= 60:
                lines.append("  ……（仅显示前 60 条）")
                break
            laddr = f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "-"
            raddr = f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "-"
            lines.append(f"  {conn.status:12} {laddr:24} → {raddr:24} pid={conn.pid}")
            count += 1
        return ToolResult(True, "\n".join(lines))

    def dns_cache(args):
        if not IS_WINDOWS:
            return ToolResult(False, "仅支持 Windows。")
        text = _run(["ipconfig", "/displaydns"], timeout=15)
        lines = [line.strip() for line in text.splitlines() if "记录名称" in line or "A (主机)" in line or "Record Name" in line]
        return ToolResult(True, "DNS 缓存条目（部分）：\n" + "\n".join(lines[:40]) if lines else "DNS 缓存为空或无法读取。")

    registry.register(
        "wifi_status", "读取当前 WiFi 连接状态：SSID、信号强度、频段、通道、收发速率。系统只读信息，不读取密码",
        {"type": "object", "properties": {}, "required": []}, wifi_status, source="netobs")
    registry.register(
        "wifi_profiles", "列出已保存的 WiFi 配置文件名。不读取也不返回任何密码/密钥",
        {"type": "object", "properties": {}, "required": []}, wifi_profiles, source="netobs")
    registry.register(
        "bluetooth_devices", "枚举本机蓝牙设备及其状态（系统 PnP 只读信息）。不嗅探蓝牙信号、不配对、不读取设备数据",
        {"type": "object", "properties": {}, "required": []}, bluetooth_devices, source="netobs")
    registry.register(
        "net_traffic", "按秒采样网卡收发字节数，给出实时上下行速率（如 KB/s）。只读聚合计数，不抓包、看不到流量内容",
        {"type": "object", "properties": {
            "seconds": {"type": "integer", "description": "采样秒数，1-10，默认 2"},
        }, "required": []}, net_traffic, source="netobs")
    registry.register(
        "net_connections", "枚举当前 TCP/UDP 网络连接（本地/远端地址、状态、PID）。只读",
        {"type": "object", "properties": {}, "required": []}, net_connections, source="netobs")
    registry.register(
        "dns_cache", "查看本机 DNS 缓存条目。只读，不改 DNS 设置",
        {"type": "object", "properties": {}, "required": []}, dns_cache, source="netobs")
