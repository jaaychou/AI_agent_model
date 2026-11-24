"""系统完全体诊断 —— 事件日志、开关机原因、硬件监控、系统配置的全面可观测。

用户要求：电脑的各种日志、系统配置日志、开关机原因、显存/显卡/内存/磁盘/CPU、
底层电源/电池等"蛛丝马迹"都能查，并自主深挖。

设计原则：
- 默认全部只读（日志/硬件/配置查询），不做破坏性修改。
- 关键能力 `system_shutdown_reason`：不满足于"查到时间"，自动深挖 Event 1074 的完整
  消息（发起进程/账户/原因）、6008（意外关机）、41（Kernel-Power）、BugCheck（蓝屏）、
  WHEA（硬件错误），综合判定"上次为什么关机"，并给出证据链。
- 硬件：GPU 用 nvidia-smi（显存/温度/功耗），电池用 WMI，磁盘用 SMART/健康状态。
- Windows 事件日志用 Get-WinEvent（FilterHashtable 高效过滤），Provider 与 ID 一起判断。
- 诚实边界：Event 41 本身不说明具体原因（断电/死机/崩溃都可能）；查不到就如实说，不编造。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .tools import ToolResult

IS_WINDOWS = os.name == "nt"

DIAG_PATTERNS = re.compile(
    r"日志|事件日志|event.?log|关机|开机|重启|重启原因|崩溃|蓝屏|bugcheck|"
    r"显存|显卡|gpu|温度|风扇|电池|电源|睡眠|唤醒|"
    r"磁盘健康|smart|硬盘|已装软件|安装.*程序|驱动|计划任务|定时任务|"
    r"系统配置|系统信息|防火墙|网络配置|时间同步|登录记录|上次.*(?:关机|开机|启动)", re.I)


def _ps(script: str, timeout: int = 45) -> str:
    if not IS_WINDOWS:
        return "仅支持 Windows"
    # 让 PowerShell 以 UTF-8 输出中文事件消息，避免 GBK 乱码
    prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + script]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip()
        if not out and r.stderr:
            return "查询失败：" + r.stderr[:500]
        return out
    except subprocess.TimeoutExpired:
        return "查询超时"
    except Exception as exc:
        return f"查询失败：{type(exc).__name__}: {exc}"


# ============ 开关机原因深挖（核心）============

_SHUTDOWN_IDS = "13,41,1074,6006,6008,6005"

def _win_events(ids: str, max_events: int = 15, days: int = 14, log: str = "System") -> str:
    script = (f"Get-WinEvent -FilterHashtable @{{LogName='{log}';Id=@({ids});"
              f"StartTime=(Get-Date).AddDays(-{days})}} -MaxEvents {max_events} "
              f"-ErrorAction SilentlyContinue | Sort-Object TimeCreated -Descending | "
              f"Select-Object TimeCreated,Id,ProviderName,Message | "
              f"Format-List | Out-String -Width 220")
    return _ps(script, timeout=60)


def _shutdown_reason(a):
    """深挖上次关机/重启的真实原因，返回证据链而非只给时间。"""
    days = max(1, min(int(a.get("days", 14) or 14), 90))
    limit = max(3, min(int(a.get("limit", 12) or 12), 40))
    evidence = []
    evidence.append("=== 关机/开机/崩溃关键事件（System 日志）===")
    ev = _win_events(_SHUTDOWN_IDS, max_events=limit, days=days)
    evidence.append(ev[:14000])

    evidence.append("\n=== 蓝屏 BugCheck ===")
    bc = _ps("Get-WinEvent -FilterHashtable @{LogName='System';Id=1001;"
             f"StartTime=(Get-Date).AddDays(-{days})" + "} -MaxEvents 5 "
             "-ErrorAction SilentlyContinue | Select-Object TimeCreated,Message | "
             "Format-List | Out-String -Width 220", timeout=60)
    evidence.append("（无蓝屏记录或：）\n" + bc[:3000])

    evidence.append("\n=== 硬件错误 WHEA-Logger ===")
    whea = _ps("Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-WHEA-Logger';"
               f"StartTime=(Get-Date).AddDays(-{days})" + "} -MaxEvents 5 -ErrorAction SilentlyContinue | "
               "Select-Object TimeCreated,Id,LevelDisplayName,Message | Format-List | Out-String -Width 220",
               timeout=60)
    evidence.append("（无硬件错误记录或：）\n" + whea[:3000])

    evidence.append("\n=== 上次启动时间 ===")
    boot = _ps("(Get-CimInstance Win32_OperatingSystem).LastBootUpTime", timeout=30)
    evidence.append(boot)

    text = "\n".join(evidence)
    # 尝试给出综合判断（基于事件 ID 的诚实推断）
    verdict = []
    if "1074" in ev and re.search(r"shutdown\.exe|winlogon|Windows Update|Task Scheduler|计划任务|用户", ev, re.I):
        verdict.append("发现 1074 计划关机/重启事件：消息里含发起进程，见上方 TimeCreated 与 Message，属主动触发的关机/重启。")
    if "6008" in ev:
        verdict.append("发现 6008：上次为意外关机（断电/强制关机/死机）——注意以 Message 里的时间为准，不是事件记录时间。")
    if " 41" in ev or "Id : 41" in ev or re.search(r"\nId\s*:\s*41", ev):
        verdict.append("发现 Kernel-Power 41：系统曾非正常重启，但 41 本身不区分断电/死机/崩溃，需结合是否有 BugCheck/WHEA 判断。")
    if bc and "BugCheck" in bc or re.search(r"0x[0-9a-fA-F]{8}", bc):
        verdict.append("发现 BugCheck 蓝屏记录：属系统崩溃导致重启，代码见上方。")
    if whea and "Error" in whea:
        verdict.append("发现 WHEA 硬件错误：可能与硬件（内存/磁盘/显卡/供电）有关。")
    if not verdict:
        verdict.append("近 %d 天未发现明确的异常关机标记；若你问的那次关机更早，请把 days 参数调大（如 days=60）重试。" % days)

    return ToolResult(True, text + "\n\n=== 综合判断（基于真实事件，非编造）===\n" +
                      "\n".join(f"- {v}" for v in verdict) +
                      "\n\n说明：Event 41 只表示「非正常重启」，不单独证明是电源/硬件问题；"
                      "如需再深挖（如具体进程、某次蓝屏代码含义），告诉我时间点我继续查对应事件。")


def _event_log_search(a):
    """通用事件日志搜索：按 log/id/关键词/天数。"""
    log = a.get("log", "System")
    ids = str(a.get("ids", "")).strip()
    keyword = str(a.get("keyword", "")).strip()
    days = max(1, min(int(a.get("days", 7) or 7), 365))
    limit = max(1, min(int(a.get("limit", 20) or 20), 100))
    if not re.fullmatch(r"[A-Za-z0-9, ]{0,60}", ids):
        return ToolResult(False, "ids 只能是数字和逗号，如 1074,41,6008。")
    id_clause = f"Id=@({ids});" if ids else ""
    kw_clause = ""
    if keyword:
        kw = keyword.replace("'", "''")
        kw_clause = (f"| Where-Object {{ $_.Message -like '*{kw}*' }}")
    script = (f"Get-WinEvent -FilterHashtable @{{LogName='{log}';{id_clause}"
              f"StartTime=(Get-Date).AddDays(-{days})}} -MaxEvents {limit} "
              f"-ErrorAction SilentlyContinue {kw_clause} | "
              f"Select-Object TimeCreated,Id,ProviderName,LevelDisplayName,Message | "
              f"Format-List | Out-String -Width 220")
    out = _ps(script, timeout=60)
    return ToolResult(True, out[:20000] or "未找到匹配的事件。")


# ============ 硬件监控 ============

def _hardware_monitor(a):
    """GPU/CPU/内存/磁盘/电池/温度的实时快照。"""
    parts = []

    # GPU（nvidia-smi）
    parts.append("=== GPU / 显存（nvidia-smi）===")
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0)
        parts.append((r.stdout or "").strip() or "nvidia-smi 无输出（可能无 NVIDIA GPU 或驱动未就绪）")
    except Exception as exc:
        parts.append(f"GPU 查询失败：{type(exc).__name__}: {exc}")

    parts.append("\n=== CPU ===")
    parts.append(_ps("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,"
                     "MaxClockSpeed,CurrentClockSpeed | Format-List | Out-String -Width 200"))

    parts.append("\n=== 内存 ===")
    parts.append(_ps("$os=Get-CimInstance Win32_OperatingSystem; "
                     "'总内存GB=' + [math]::Round($os.TotalVisibleMemorySize/1MB,2) + "
                     "' 可用GB=' + [math]::Round($os.FreePhysicalMemory/1MB,2)"))

    parts.append("\n=== 磁盘 ===")
    parts.append(_ps("Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
                     "Select-Object DeviceID,VolumeName,@{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}},"
                     "@{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB,1)}} | Format-Table -AutoSize | Out-String -Width 200"))

    parts.append("\n=== 电池 / 电源 ===")
    parts.append(_ps("Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | "
                     "Select-Object Name,EstimatedChargeRemaining,BatteryStatus | Format-List | Out-String -Width 200; "
                     "powercfg /getactivescheme"))

    parts.append("\n=== 温度（若 WMI 提供）===")
    parts.append(_ps("Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                     "-ErrorAction SilentlyContinue | Select-Object -First 5 InstanceName,"
                     "@{n='TempC';e={[math]::Round(($_.CurrentTemperature/10)-273.15,1)}} | "
                     "Format-Table -AutoSize | Out-String -Width 200"))

    return ToolResult(True, "\n".join(parts)[:22000])


# ============ 系统配置可观测 ============

def _installed_software(a):
    kw = str(a.get("keyword", "")).strip()
    where = ""
    if kw:
        k = kw.replace("'", "''")
        where = f"| Where-Object {{ $_.DisplayName -like '*{k}*' }}"
    script = ("Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*,"
              "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* "
              "-ErrorAction SilentlyContinue | Where-Object { $_.DisplayName } " + where +
              " | Select-Object DisplayName,DisplayVersion,Publisher | Sort-Object DisplayName | "
              "Format-Table -AutoSize | Out-String -Width 200")
    return ToolResult(True, _ps(script, timeout=60)[:20000])


def _scheduled_tasks(a):
    kw = str(a.get("keyword", "")).strip()
    script = ("Get-ScheduledTask -ErrorAction SilentlyContinue | "
              "Select-Object TaskName,State,@{n='Actions';e={($_.Actions.Execute -join '; ')}} | "
              "Format-Table -AutoSize | Out-String -Width 240")
    out = _ps(script, timeout=60)
    if kw:
        lines = [l for l in out.splitlines() if kw.casefold() in l.casefold()]
        out = "\n".join(lines) or f"未找到含 '{kw}' 的计划任务"
    return ToolResult(True, out[:20000])


def _drivers(a):
    kw = str(a.get("keyword", "")).strip()
    where = f"| Where-Object {{ $_.Name -like '*{kw.replace(chr(39), chr(39)*2)}*' }}" if kw else ""
    script = ("Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | "
              "Where-Object { $_.DeviceName } " + where +
              " | Select-Object DeviceName,DriverVersion,Manufacturer,DriverDate | "
              "Sort-Object DeviceName | Format-Table -AutoSize | Out-String -Width 220")
    return ToolResult(True, _ps(script, timeout=60)[:20000])


def _network_config(a):
    parts = [
        "=== 网络适配器 IP 配置 ===",
        _ps("Get-NetIPConfiguration | Select-Object InterfaceAlias,IPv4Address,IPv6Address,"
            "IPv4DefaultGateway,DNSServer | Format-List | Out-String -Width 200"),
        "\n=== 防火墙状态 ===",
        _ps("Get-NetFirewallProfile | Select-Object Name,Enabled | Format-Table -AutoSize | Out-String -Width 120"),
        "\n=== 时间同步 ===",
        _ps("w32tm /query /status | Out-String -Width 200"),
    ]
    return ToolResult(True, "\n".join(parts)[:20000])


def _login_history(a):
    days = max(1, min(int(a.get("days", 7) or 7), 90))
    script = (f"Get-WinEvent -FilterHashtable @{{LogName='Security';Id=4624,4625;"
              f"StartTime=(Get-Date).AddDays(-{days})}} -MaxEvents 25 -ErrorAction SilentlyContinue | "
              "Select-Object TimeCreated,Id,@{n='User';e={$_.Properties[5].Value}},"
              "@{n='IP';e={$_.Properties[18].Value}} | Format-Table -AutoSize | Out-String -Width 200")
    out = _ps(script, timeout=60)
    return ToolResult(True, out[:12000] or "无登录记录（Security 日志可能需要管理员权限访问）")


def _system_overview_config(a):
    parts = [
        "=== 操作系统 ===",
        _ps("Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,"
            "OSArchitecture,InstallDate,LastBootUpTime | Format-List | Out-String -Width 200"),
        "\n=== 计算机 ===",
        _ps("Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,"
            "TotalPhysicalMemory,Domain,PartOfDomain | Format-List | Out-String -Width 200"),
        "\n=== 电源计划 ===",
        _ps("powercfg /list | Out-String -Width 200"),
    ]
    return ToolResult(True, "\n".join(parts)[:14000])


def register_system_diagnostics(registry):
    registry.register(
        "system_shutdown_reason",
        "深挖『上次为什么关机/重启』的真实原因。自动查 System 事件日志的 1074(谁发起关机)、"
        "6005/6006(开机/正常关机)、6008(意外关机)、41(Kernel-Power)、BugCheck(蓝屏)、WHEA(硬件错误)，"
        "综合给出证据链，而不是只报时间。用户问『上次为啥关机』『电脑为什么重启』『是不是断电/蓝屏』时用。",
        {"type": "object", "properties": {
            "days": {"type": "integer", "description": "查最近多少天，默认14，最大90"},
            "limit": {"type": "integer", "description": "最多返回多少条事件，默认12"}
        }},
        _shutdown_reason, source="diagnostics")

    registry.register(
        "event_log_search",
        "通用 Windows 事件日志搜索：可指定 log(System/Application/Security)、ids(如 1074,41,6008)、"
        "keyword(消息关键词)、days、limit。查各种系统日志蛛丝马迹用。",
        {"type": "object", "properties": {
            "log": {"type": "string", "description": "日志名：System/Application/Security"},
            "ids": {"type": "string", "description": "事件ID，逗号分隔，如 1074,41"},
            "keyword": {"type": "string", "description": "消息中关键词"},
            "days": {"type": "integer"}, "limit": {"type": "integer"}
        }},
        _event_log_search, source="diagnostics")

    registry.register(
        "hardware_monitor",
        "实时硬件快照：GPU 型号/显存占用/温度/功耗(nvidia-smi)、CPU、内存、磁盘、电池/电源计划、温度。"
        "用户问显存/显卡状态/温度/电池/当前硬件负载时用。",
        {"type": "object", "properties": {}},
        _hardware_monitor, source="diagnostics")

    registry.register(
        "installed_software",
        "列出本机已安装软件（名称/版本/发布者），可按 keyword 筛选。用户问装了什么软件/某软件版本时用。",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "可选：软件名关键词"}
        }},
        _installed_software, source="diagnostics")

    registry.register(
        "scheduled_tasks",
        "列出 Windows 计划任务（名称/状态/执行动作），可按 keyword 筛选。用户问定时任务/开机自动执行/计划任务时用。",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "可选：任务名关键词"}
        }},
        _scheduled_tasks, source="diagnostics")

    registry.register(
        "driver_list",
        "列出已安装驱动程序（设备名/驱动版本/厂商/日期），可按 keyword 筛选。查驱动版本/硬件识别用。",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "可选：设备名关键词"}
        }},
        _drivers, source="diagnostics")

    registry.register(
        "network_config",
        "查看网络配置：适配器 IP/DNS/网关、防火墙状态、时间同步状态。",
        {"type": "object", "properties": {}},
        _network_config, source="diagnostics")

    registry.register(
        "login_history",
        "查看近期登录记录（Security 日志 4624 登录/4625 失败），含账户与来源 IP。可能需要管理员权限。",
        {"type": "object", "properties": {
            "days": {"type": "integer", "description": "最近多少天，默认7"}
        }},
        _login_history, source="diagnostics")

    registry.register(
        "system_config_overview",
        "系统配置总览：操作系统版本/安装日期/上次启动、制造商型号、电源计划列表。",
        {"type": "object", "properties": {}},
        _system_overview_config, source="diagnostics")


def has_diag_intent(text: str) -> bool:
    return bool(DIAG_PATTERNS.search(str(text or "")))
