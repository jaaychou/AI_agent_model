"""网络抓包与无线嗅探工具 —— 受授权开关门控，依赖 Npcap + 管理员权限。

诚实边界（务必看）：
- 抓包需要 Npcap 驱动（本机已装服务）+ 以管理员身份运行 Agent。工具会检测并如实报告。
- WiFi 802.11 监控模式：绝大多数 Windows 商用网卡驱动**不支持** monitor mode，
  即使 Npcap 也不行。工具会尝试并明确告诉用户硬件是否支持，不编造抓到的帧。
- 蓝牙嗅探：Windows 上 scapy 无法直接进 BLE 嗅探模式；提供的是"列出BLE广播设备（若系统支持）"
  与降级说明，真正抓 BLE 空口包需专用硬件（如 nRF Sniffer / Ubertooth）。
- 读取已保存 WiFi 密码：netsh 可导出明文，但这属于敏感凭据，必须管理员 + 用户确认，
  且只显示 SSID 与密码，不外传。
本模块所有工具默认不注册，只有 GUI 抓包授权开关打开后才可用。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .tools import ToolResult

IS_WINDOWS = os.name == "nt"
BASE = Path(__file__).resolve().parent.parent
VENDOR = BASE / "runtime" / "vendor"
CAPTURE_DIR = BASE / "data" / "captures"

CAPTURE_PATTERNS = re.compile(
    r"抓包|嗅探|monitor模式|监控模式|802\.11|无线帧|蓝牙.*(?:抓|嗅探|广播)|"
    r"ble.*(?:scan|sniff|嗅)|packet capture|tcpdump|wifi.*密码|无线密码", re.I)


def _ensure_vendor_on_path():
    if VENDOR.exists() and str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))


def _try_import_scapy():
    _ensure_vendor_on_path()
    try:
        from scapy.all import get_if_list  # noqa: F401
        return True, ""
    except Exception as exc:
        return False, f"scapy 不可用：{type(exc).__name__}: {exc}"


def _is_admin():
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _npcap_status():
    if not IS_WINDOWS:
        return False, "仅 Windows"
    try:
        proc = subprocess.run(["sc", "query", "npcap"], capture_output=True, text=True,
                              timeout=10, encoding="utf-8", errors="replace")
        running = "RUNNING" in (proc.stdout or "").upper()
        return running, ("运行中" if running else "服务存在但未运行")
    except Exception as exc:
        return False, str(exc)


def _env_check():
    """返回 (ok, 说明)。"""
    ok, scapy_err = _try_import_scapy()
    admin = _is_admin()
    npcap, npcap_note = _npcap_status()
    lines = [
        f"管理员权限：{'是' if admin else '否（抓包必须用管理员身份重启 Agent）'}",
        f"Npcap 驱动：{'可用' if npcap else '不可用（' + npcap_note + '）'}",
        f"scapy：{'可用' if ok else scapy_err}",
    ]
    return ok and admin and npcap, "\n".join(lines)


def _capture_packets(args, confirm):
    iface = (args.get("interface") or "").strip()
    count = int(args.get("count", 20) or 20)
    count = max(1, min(count, 200))
    bpf = (args.get("filter") or "").strip()
    duration = args.get("duration_s")
    if not confirm(f"开始网络抓包：接口={iface or '默认'}，数量={count}，过滤={bpf or '无'}。"
                   f"\n抓到的包仅保存在本机 agent/data/captures，不外传。是否继续？"):
        return ToolResult(False, "用户取消了抓包。")
    ok, note = _env_check()
    if not ok:
        return ToolResult(False, "抓包环境未就绪：\n" + note +
                          "\n请以管理员身份重启 Agent 后重试（Npcap 已装）。")
    try:
        from scapy.all import sniff, wrpcap
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        kwargs = {"count": count, "iface": iface or None}
        if bpf:
            kwargs["filter"] = bpf
        if duration:
            kwargs["timeout"] = max(1, int(duration))
        pkts = sniff(**kwargs)
        out = CAPTURE_DIR / f"capture_{len(list(CAPTURE_DIR.glob('capture_*.pcap')))+1}.pcap"
        wrpcap(str(out), pkts)
        summary = []
        for p in list(pkts)[:count]:
            try:
                layers = "+".join(ls.name for ls in p.layers())
                src = getattr(p, "src", "?")
                dst = getattr(p, "dst", "?")
                summary.append(f"{layers} {src}->{dst}")
            except Exception:
                pass
        return ToolResult(True, f"抓到 {len(pkts)} 个包，已保存：{out}\n" +
                          "\n".join(summary[:30]) + ("\n..." if len(summary) > 30 else ""))
    except Exception as exc:
        return ToolResult(False, f"抓包失败：{type(exc).__name__}: {exc}\n环境：\n{note}")


def _scan_wifi_80211(args, confirm):
    """尝试 WiFi 802.11 监控模式扫描，硬件不支持则诚实降级。"""
    if not confirm("尝试将 WiFi 网卡切换到监控模式扫描 802.11 帧（可能短暂断开 WiFi）。是否继续？"):
        return ToolResult(False, "用户取消了 WiFi 嗅探。")
    ok, note = _env_check()
    if not ok:
        return ToolResult(False, "环境未就绪：\n" + note)
    # Windows 商用网卡几乎都不支持 monitor mode；尝试列出接口并探测
    try:
        from scapy.all import get_if_list
        try:
            from scapy.arch.windows import get_windows_if_list
            ifaces = get_windows_if_list()
            info = "\n".join(
                f"- {i.get('name')} | {i.get('description', '')[:80]}" for i in ifaces[:15])
        except Exception:
            info = "\n".join(f"- {x}" for x in get_if_list()[:15])
        return ToolResult(
            True,
            "检测到以下网络接口：\n" + info +
            "\n\n诚实说明：Windows 下的商用 WiFi 网卡驱动绝大多数**不支持 802.11 监控模式**，"
            "即使装了 Npcap 也无法进入 monitor mode 抓取无线帧。"
            "要真正嗅探 WiFi 空口帧，需要支持 monitor 模式的网卡（如某些 USB 无线网卡 + 特定驱动）"
            "或 Linux + 兼容驱动。本工具不会伪造抓到的帧；若你的硬件支持，请告诉我网卡型号，我针对性配置。")
    except Exception as exc:
        return ToolResult(False, f"探测 WiFi 接口失败：{type(exc).__name__}: {exc}\n{note}")


def _scan_ble_beacon(args, confirm):
    """列出附近 BLE 广播设备（系统支持时），否则诚实说明需专用硬件。"""
    if not confirm("扫描附近 BLE 蓝牙广播设备（只读）。是否继续？"):
        return ToolResult(False, "用户取消了 BLE 扫描。")
    if not IS_WINDOWS:
        return ToolResult(False, "BLE 扫描目前仅实现 Windows 版。")
    # 用 PowerShell + 蓝牙 PnP 枚举已配对/已发现设备；空口广播帧需专用硬件
    script = (
        "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        "Where-Object { $_.FriendlyName } | Select-Object FriendlyName,Status,InstanceId | "
        "Format-Table -AutoSize | Out-String -Width 200"
    )
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=25,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "").strip() or "未发现蓝牙设备"
        return ToolResult(True, out[:6000] +
                          "\n\n诚实说明：以上是 Windows 已枚举的蓝牙设备，不是空口 BLE 广播帧。"
                          "真正抓 BLE 广播包需要专用嗅探硬件（nRF Sniffer/Ubertooth 等）；"
                          "scapy 在 Windows 上无法直接进入 BLE 嗅探模式。若你有这类硬件，告诉我型号我接入。")
    except Exception as exc:
        return ToolResult(False, f"扫描蓝牙失败：{type(exc).__name__}: {exc}")


def _wifi_profiles_key(args, confirm):
    """导出已保存 WiFi 的密码（需管理员 + 确认），不外传。"""
    if not confirm("读取本机已保存 WiFi 网络的密码（需管理员）。这些是敏感凭据，"
                   "仅显示在你眼前、不外传。是否继续？"):
        return ToolResult(False, "用户取消了读取 WiFi 密码。")
    if not _is_admin():
        return ToolResult(False, "需要管理员权限：请以管理员身份重启 Agent 后重试。")
    try:
        prof = subprocess.run(["netsh", "wlan", "show", "profiles"], capture_output=True,
                              text=True, timeout=20, encoding="utf-8", errors="replace")
        ssids = re.findall(r"All User Profile\s*:\s*(.+)", prof.stdout or "")
        rows = []
        for ssid in ssids[:30]:
            ssid = ssid.strip()
            detail = subprocess.run(["netsh", "wlan", "show", "profile", ssid, "key=clear"],
                                    capture_output=True, text=True, timeout=15,
                                    encoding="utf-8", errors="replace")
            m = re.search(r"Key Content\s*:\s*(.+)", detail.stdout or "")
            rows.append(f"{ssid} : {m.group(1).strip() if m else '(无密码/无法读取)'}")
        return ToolResult(True, "\n".join(rows) or "未找到已保存的 WiFi 配置")
    except Exception as exc:
        return ToolResult(False, f"读取失败：{type(exc).__name__}: {exc}")


def register_capture_tools(registry, confirm=None, enabled: bool = False):
    """enabled=False 时不注册任何抓包工具（GUI 授权开关门控）。"""
    if not enabled:
        return
    confirm = confirm or (lambda _m: False)

    def capture_status(a):
        ok, note = _env_check()
        return ToolResult(True, ("抓包环境就绪" if ok else "抓包环境未就绪") + "\n" + note)

    registry.register(
        "capture_status",
        "检查本机抓包环境是否就绪：管理员权限、Npcap 驱动、scapy 库。做抓包前先调用它。",
        {"type": "object", "properties": {}},
        capture_status, source="capture")

    registry.register(
        "capture_packets",
        "用 Npcap 抓网络包并保存为 pcap 文件（本机 agent/data/captures）。需管理员权限。"
        "参数 interface 网卡名、count 包数(默认20)、filter BPF过滤(如 tcp port 80)、duration_s 秒数。每次需确认。",
        {"type": "object", "properties": {
            "interface": {"type": "string", "description": "网卡名，留空用默认"},
            "count": {"type": "integer", "description": "抓取包数，默认20，最多200"},
            "filter": {"type": "string", "description": "BPF 过滤表达式，如 tcp"},
            "duration_s": {"type": "integer", "description": "抓包时长秒（可选）"}
        }},
        lambda a: _capture_packets(a, confirm), source="capture")

    registry.register(
        "scan_wifi_80211",
        "尝试扫描 WiFi 802.11 帧/列出无线接口。诚实说明：Windows 商用网卡多数不支持 monitor 模式，"
        "工具会检测并如实报告，不伪造抓到的帧。需确认。",
        {"type": "object", "properties": {}},
        lambda a: _scan_wifi_80211(a, confirm), source="capture")

    registry.register(
        "scan_ble_beacon",
        "列出 Windows 已枚举的蓝牙设备。真正抓 BLE 空口广播帧需专用硬件(nRF/Ubertooth)，"
        "本工具会如实说明边界。需确认。",
        {"type": "object", "properties": {}},
        lambda a: _scan_ble_beacon(a, confirm), source="capture")

    registry.register(
        "wifi_profile_passwords",
        "读取本机已保存 WiFi 网络的密码（需管理员 + 用户确认）。仅本地显示，不外传。"
        "用户明确要查自己电脑保存的 WiFi 密码时用。",
        {"type": "object", "properties": {}},
        lambda a: _wifi_profiles_key(a, confirm), source="capture")


def has_capture_intent(text: str) -> bool:
    return bool(CAPTURE_PATTERNS.search(text))
