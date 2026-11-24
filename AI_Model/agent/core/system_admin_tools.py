"""系统底层"增删改查"受控扩展。

用户要求：不只是查日志/硬件，还要能增删改系统底层配置。
但注册表、计划任务、环境变量、电源计划这类写操作一旦失控，足以让系统无法启动或
造成持久化后门，所以本模块的设计是：

1. 只读深挖工具（windows_forensics）默认可用；
2. 写工具必须在 GUI 打开"系统配置管理"开关后才注册，且每次写操作都走 confirm；
3. 注册表写入限制在明确白名单路径，绝不开放整棵 HKLM/HKCU；
4. 计划任务的触发器走白名单，不接受任意原始 schtasks 参数串；
5. 命令行全部用 argv 列表传递，不拼 shell，避免注入；
6. 诚实边界：Machine 范围环境变量、HKLM 写入仍受当前账户权限和 UAC 限制。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .tools import ToolResult

IS_WINDOWS = os.name == "nt"

SYSTEM_ADMIN_PATTERNS = re.compile(
    r"电源计划|功耗模式|节能模式|高性能模式|唤醒源|上次唤醒|更新历史|补丁|"
    r"蓝屏历史|软件崩溃|磁盘健康|smart|显存占用进程|计划任务|定时任务|"
    r"环境变量|注册表|系统配置.*(?:改|设置|添加|删除)|增删改查", re.I)

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\\-][A-Za-z0-9_.\\ -]{0,254}$")
_SECRET_HINT = re.compile(r"pass|pwd|token|secret|key|credential|api.?key", re.I)

# 注册表只在这些路径上开放，防止模型把任意 HKLM 配置改坏。
_REGISTRY_ALLOW = (
    r"HKCU\Software\LocalAgent",
    r"HKLM\Software\LocalAgent",
    r"HKCU\Control Panel\Desktop",
)
_REG_TYPE = {
    "string": "REG_SZ",
    "dword": "REG_DWORD",
    "qword": "REG_QWORD",
    "expand": "REG_EXPAND_SZ",
}
_MACHINE_ENV_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"


def _run(argv: list[str], timeout: int = 30, encoding: str | None = None) -> tuple[int, str, str]:
    """统一命令执行：argv 传参，不经 shell。

    PowerShell 入口会强制 UTF-8 输出，走 utf-8；powercfg/reg/schtasks 这些原生命令
    在中文 Windows 上走 ANSI/OEM 代码页，必须用 mbcs 解码，否则全是乱码。
    """
    if not IS_WINDOWS:
        return 1, "", "仅支持 Windows"
    enc = encoding or ("mbcs" if IS_WINDOWS else "utf-8")
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            encoding=enc, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 1, "", f"命令超时（{timeout}s）"
    except Exception as exc:
        return 1, "", f"命令执行失败：{type(exc).__name__}: {exc}"
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def _ps(script: str, timeout: int = 45) -> tuple[int, str, str]:
    prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + script],
                timeout, encoding="utf-8")


def _block(title: str, argv_or_script, timeout: int = 45) -> str:
    if isinstance(argv_or_script, str):
        code, out, err = _ps(argv_or_script, timeout)
    else:
        code, out, err = _run(argv_or_script, timeout)
    text = out or ""
    if err and err not in text:
        text += ("\n" if text else "") + "[stderr] " + err
    if code != 0 and not text:
        text = f"[退出码 {code}] 未返回输出"
    return f"--- {title} ---\n{text or '(空)'}"


def _confirmed(confirm, message: str) -> bool:
    if confirm is None:
        return False
    try:
        return bool(confirm(message))
    except Exception:
        return False


def _mask(text: str) -> str:
    """环境变量值里疑似凭据的字段脱敏，避免把 token 回显给用户或模型。"""
    lines = []
    for line in (text or "").splitlines():
        if _SECRET_HINT.search(line) and "=" in line:
            key, _, _val = line.partition("=")
            lines.append(f"{key}=[已隐藏]")
        else:
            lines.append(line)
    return "\n".join(lines)


# ============ 只读：更深的底层取证 ============

def _windows_forensics(a):
    kind = str(a.get("kind", "all") or "all").strip().lower()
    days = max(1, min(int(a.get("days", 14) or 14), 90))
    allowed = {"all", "wake", "update", "reliability", "smart", "gpu_processes"}
    if kind not in allowed:
        return ToolResult(False, f"kind 不支持：{kind}。可用：{', '.join(sorted(allowed))}")

    blocks = []
    if kind in ("all", "wake"):
        blocks.append(_block("powercfg /lastwake（上次唤醒源）", ["powercfg", "/lastwake"]))
        blocks.append(_block("powercfg /devicequery wake_armed（当前允许唤醒的设备）",
                             ["powercfg", "/devicequery", "wake_armed"]))
        blocks.append(_block("powercfg /requests（阻止睡眠的进程）", ["powercfg", "/requests"]))
        blocks.append(_block("powercfg /systempowerreport（近期睡眠/唤醒摘要）",
                             ["powercfg", "/systempowerreport", "/output", str(Path(os.environ.get("TEMP", ".")) / "agent-powerreport.html")]))
    if kind in ("all", "update"):
        blocks.append(_block(
            f"最近 {days} 天 Windows 更新/补丁",
            f"Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 30 "
            f"HotFixID,InstalledOn,Description | Format-Table -AutoSize | Out-String -Width 200"))
    if kind in ("all", "reliability"):
        blocks.append(_block(
            f"最近 {days} 天应用崩溃/Windows 错误报告",
            f"Get-WinEvent -FilterHashtable @{{LogName='Application';Id=@(1000,1001,1002);"
            f"StartTime=(Get-Date).AddDays(-{days})}} -MaxEvents 40 -ErrorAction SilentlyContinue | "
            f"Sort-Object TimeCreated -Descending | Select-Object TimeCreated,Id,ProviderName,Message | "
            f"Format-List | Out-String -Width 220"))
    if kind in ("all", "smart"):
        blocks.append(_block(
            "磁盘 SMART / 可靠性计数",
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | ForEach-Object { "
            "$d=$_; $r=$d | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; "
            "[pscustomobject]@{Device=$d.FriendlyName;Media=$d.MediaType;Health=$d.HealthStatus;"
            "TempC=$r.Temperature;ReadErrors=$r.ReadErrorsTotal;WriteErrors=$r.WriteErrorsTotal;"
            "Wear=$r.Wear} } | Format-Table -AutoSize | Out-String -Width 240"))
    if kind in ("all", "gpu_processes"):
        blocks.append(_block(
            "GPU 进程占用（nvidia-smi compute apps）",
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv"]))
        blocks.append(_block("GPU 总览", ["nvidia-smi"]))

    return ToolResult(True, "\n\n".join(blocks)[:20000])


# ============ 写：电源计划 ============

def _power_plan_manager(a, confirm):
    action = str(a.get("action", "list") or "list").strip().lower()
    guid = str(a.get("guid", "") or "").strip()
    name = str(a.get("name", "") or "").strip()

    if action == "list":
        code, out, err = _run(["powercfg", "/list"])
        return ToolResult(code == 0, (out or err or "无电源计划").strip()[:8000])

    if action == "create":
        base = guid if guid else "SCHEME_CURRENT"
        if guid and not _GUID_RE.fullmatch(guid):
            return ToolResult(False, "guid 必须是完整 UUID 格式，或留空从当前方案复制。")
        msg = f"创建新的电源计划（复制自 {base}）" + (f"，命名为“{name}”" if name else "") + "。是否继续？"
        if not _confirmed(confirm, msg):
            return ToolResult(False, "用户未授权创建电源计划。")
        code, out, err = _run(["powercfg", "/duplicatescheme", base])
        text = (out or err or "").strip()
        if code != 0:
            return ToolResult(False, f"创建失败：{text[:1000]}")
        if name:
            found = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", text)
            if found:
                ncode, nout, nerr = _run(["powercfg", "/changename", found.group(1), name])
                if ncode == 0:
                    text += f"\n已重命名：{name}"
                else:
                    text += f"\n重命名失败（方案已创建）：{(nerr or nout)[:500]}"
        return ToolResult(True, text[:4000])

    if not _GUID_RE.fullmatch(guid):
        return ToolResult(False, "该操作需要 guid 参数（完整 UUID，可从 list 结果获取）。")

    if action in ("set", "delete"):
        code, out, err = _run(["powercfg", "/list"])
        listing = out or err
        if guid.lower() not in listing.lower():
            return ToolResult(False, "该 GUID 不在本机电源计划列表中，已拒绝操作。")
        active_line = next((ln for ln in listing.splitlines() if guid.lower() in ln.lower()), "")
        if action == "delete" and active_line.rstrip().endswith("*"):
            return ToolResult(False, "该电源计划当前正在使用，不能删除。请先 set 到别的方案。")

    messages = {
        "set": f"切换到电源计划 {guid}。是否继续？",
        "rename": f"将电源计划 {guid} 重命名为“{name}”。是否继续？",
        "delete": f"删除电源计划 {guid}。此操作不可撤销。是否继续？",
    }
    if action not in messages:
        return ToolResult(False, f"action 不支持：{action}。可用：list/create/set/rename/delete")
    if action == "rename" and not name:
        return ToolResult(False, "rename 需要 name 参数。")
    if not _confirmed(confirm, messages[action]):
        return ToolResult(False, "用户未授权修改电源计划。")

    argv = {
        "set": ["powercfg", "/setactive", guid],
        "rename": ["powercfg", "/changename", guid, name],
        "delete": ["powercfg", "/delete", guid],
    }[action]
    code, out, err = _run(argv)
    text = (out or err or "操作完成").strip()
    return ToolResult(code == 0, (text or "操作完成")[:4000])


# ============ 写：计划任务 ============

def _scheduled_task_manager(a, confirm):
    action = str(a.get("action", "list") or "list").strip().lower()
    task = str(a.get("name", "") or "").strip()
    command = str(a.get("command", "") or "").strip()
    trigger = str(a.get("trigger", "ONLOGON") or "ONLOGON").strip().upper()
    start_time = str(a.get("time", "") or "").strip()

    if action == "list":
        code, out, err = _run(["schtasks", "/Query", "/FO", "LIST", "/V"], timeout=60)
        text = (out or err or "无计划任务")
        keyword = str(a.get("keyword", "") or "").strip().lower()
        if keyword:
            chunks = [c for c in text.split("\n\n") if keyword in c.lower()]
            text = "\n\n".join(chunks) or f"没有匹配“{keyword}”的计划任务"
        return ToolResult(code == 0, text[:16000])

    if action == "detail":
        if not _TASK_NAME_RE.fullmatch(task):
            return ToolResult(False, "任务名不合法。")
        code, out, err = _run(["schtasks", "/Query", "/TN", task, "/FO", "LIST", "/V"])
        return ToolResult(code == 0, (out or err)[:12000])

    if not _TASK_NAME_RE.fullmatch(task):
        return ToolResult(False, "任务名不合法（只允许字母、数字、点、下划线、连字符、反斜杠和空格）。")

    if action == "create":
        if not command or "\n" in command or "\r" in command:
            return ToolResult(False, "create 需要单行 command 参数。")
        if trigger not in {"DAILY", "WEEKLY", "MONTHLY", "HOURLY", "ONLOGON", "ONSTART"}:
            return ToolResult(False, "trigger 必须是 DAILY/WEEKLY/MONTHLY/HOURLY/ONLOGON/ONSTART 之一。")
        argv = ["schtasks", "/Create", "/TN", task, "/TR", command, "/SC", trigger, "/F"]
        if start_time:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start_time):
                return ToolResult(False, "time 必须是 HH:MM（24 小时制）。")
            argv += ["/ST", start_time]
        if not _confirmed(confirm, f"创建计划任务：\n名称：{task}\n命令：{command}\n触发器：{trigger}\n是否继续？"):
            return ToolResult(False, "用户未授权创建计划任务。")
        code, out, err = _run(argv)
        return ToolResult(code == 0, (out or err or "创建成功")[:4000])

    argv_map = {
        "delete": ["schtasks", "/Delete", "/TN", task, "/F"],
        "run": ["schtasks", "/Run", "/TN", task],
        "enable": ["schtasks", "/Change", "/TN", task, "/ENABLE"],
        "disable": ["schtasks", "/Change", "/TN", task, "/DISABLE"],
    }
    if action not in argv_map:
        return ToolResult(False, f"action 不支持：{action}。可用：list/detail/create/delete/run/enable/disable")
    verb = {"delete": "删除", "run": "立即运行", "enable": "启用", "disable": "禁用"}[action]
    suffix = "（删除不可撤销）" if action == "delete" else ""
    if not _confirmed(confirm, f"{verb}计划任务 {task}{suffix}。是否继续？"):
        return ToolResult(False, f"用户未授权{verb}计划任务。")
    code, out, err = _run(argv_map[action])
    return ToolResult(code == 0, (out or err or f"{verb}成功")[:4000])


# ============ 写：环境变量 ============

def _env_key(scope: str) -> str:
    return r"HKCU\Environment" if scope == "user" else _MACHINE_ENV_KEY


def _env_var_manager(a, confirm):
    action = str(a.get("action", "list") or "list").strip().lower()
    scope = str(a.get("scope", "user") or "user").strip().lower()
    name = str(a.get("name", "") or "").strip()
    value = str(a.get("value", "") or "")
    if scope not in ("user", "machine"):
        return ToolResult(False, "scope 只能是 user 或 machine。")

    key = _env_key(scope)
    if action in ("list", "get"):
        argv = ["reg", "query", key]
        if action == "get":
            if not _NAME_RE.fullmatch(name):
                return ToolResult(False, "变量名不合法。")
            argv += ["/v", name]
        code, out, err = _run(argv)
        return ToolResult(code == 0, _mask(out or err or "(空)")[:12000])

    if action not in ("set", "delete"):
        return ToolResult(False, f"action 不支持：{action}。可用：list/get/set/delete")
    if not _NAME_RE.fullmatch(name):
        return ToolResult(False, "变量名不合法（字母/数字/下划线，且不能以数字开头）。")
    if scope == "machine" and not _is_admin():
        return ToolResult(False, "修改 Machine 范围环境变量需要管理员权限。请以管理员身份启动 GUI。")

    if action == "set":
        if not _confirmed(confirm, f"设置 {scope} 环境变量 {name}（值已隐藏，可能含敏感信息）。是否继续？"):
            return ToolResult(False, "用户未授权修改环境变量。")
        argv = ["reg", "add", key, "/v", name, "/t", "REG_SZ", "/d", value, "/f"]
    else:
        if not _confirmed(confirm, f"删除 {scope} 环境变量 {name}。是否继续？"):
            return ToolResult(False, "用户未授权删除环境变量。")
        argv = ["reg", "delete", key, "/v", name, "/f"]
    code, out, err = _run(argv)
    return ToolResult(code == 0, (out or err or "操作完成")[:3000])


# ============ 写：注册表（严格白名单） ============

def _is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _normalize_reg_path(text: str) -> str:
    p = str(text or "").strip().strip('"')
    if not re.fullmatch(r"[A-Za-z0-9_\\ .-]+", p):
        raise ValueError("注册表路径含非法字符。")
    parts = p.split("\\")
    if parts[0].upper() in ("HKCU", "HKEY_CURRENT_USER"):
        hive = "HKCU"
    elif parts[0].upper() in ("HKLM", "HKEY_LOCAL_MACHINE"):
        hive = "HKLM"
    else:
        raise ValueError("只支持 HKCU / HKLM。")
    sub = "\\".join(parts[1:]).strip("\\")
    candidate = f"{hive}\\{sub}" if sub else hive
    for allowed in _REGISTRY_ALLOW:
        if candidate.lower() == allowed.lower() or candidate.lower().startswith(allowed.lower() + "\\"):
            return candidate
    raise ValueError("该注册表路径不在允许列表中；只允许 " + "、".join(_REGISTRY_ALLOW))


def _registry_manager(a, confirm):
    action = str(a.get("action", "list") or "list").strip().lower()
    try:
        full = _normalize_reg_path(str(a.get("path", "") or ""))
    except ValueError as exc:
        return ToolResult(False, str(exc))
    name = str(a.get("name", "") or "").strip()
    value = str(a.get("value", "") or "")
    rtype = str(a.get("type", "string") or "string").strip().lower()
    if full.startswith("HKLM\\") and not _is_admin():
        return ToolResult(False, "写入 HKLM 需要管理员权限。")

    if action in ("list", "get"):
        argv = ["reg", "query", full]
        if action == "get":
            if not name or not re.fullmatch(r"[A-Za-z0-9_ .-]+", name):
                return ToolResult(False, "name 不合法。")
            argv += ["/v", name]
        code, out, err = _run(argv)
        return ToolResult(code == 0, _mask(out or err or "(空)")[:12000])

    if action not in ("set", "delete"):
        return ToolResult(False, f"action 不支持：{action}。可用：list/get/set/delete")
    if not name or not re.fullmatch(r"[A-Za-z0-9_ .-]+", name):
        return ToolResult(False, "name 不合法。")

    if action == "set":
        reg_type = _REG_TYPE.get(rtype)
        if not reg_type:
            return ToolResult(False, f"type 不支持：{rtype}。可用：{', '.join(_REG_TYPE)}")
        if not _confirmed(confirm, f"写入注册表 {full} 的 {name}（值已隐藏）。是否继续？"):
            return ToolResult(False, "用户未授权写入注册表。")
        argv = ["reg", "add", full, "/v", name, "/t", reg_type, "/d", value, "/f"]
    else:
        if not _confirmed(confirm, f"删除注册表 {full} 下的值 {name}。此操作不可撤销。是否继续？"):
            return ToolResult(False, "用户未授权删除注册表值。")
        argv = ["reg", "delete", full, "/v", name, "/f"]
    code, out, err = _run(argv)
    return ToolResult(code == 0, (out or err or "操作完成")[:3000])


def has_system_admin_intent(text: str) -> bool:
    return bool(SYSTEM_ADMIN_PATTERNS.search(str(text or "")))


def register_system_admin_tools(registry, confirm=None, enabled: bool = False):
    """windows_forensics 默认可用；写工具仅在 enabled=True 时注册。"""
    registry.register(
        "windows_forensics",
        "更深的 Windows 底层取证：kind=wake/update/reliability/smart/gpu_processes/all。"
        "查上次唤醒源、阻止睡眠的进程、补丁历史、应用崩溃、磁盘 SMART、GPU 进程占用。只读。",
        {"type": "object", "properties": {
            "kind": {"type": "string", "description": "wake/update/reliability/smart/gpu_processes/all"},
            "days": {"type": "integer", "description": "更新/崩溃查询天数，默认14"}
        }},
        _windows_forensics, source="diagnostics")

    if not enabled:
        return

    registry.register(
        "power_plan_manager",
        "电源计划增删改查：action=list/create/set/rename/delete。切换/创建/删除需 guid，"
        "所有写操作都会请求确认；不能删除当前正在使用的方案。Windows 电源计划由 powercfg 管理。",
        {"type": "object", "properties": {
            "action": {"type": "string", "description": "list/create/set/rename/delete"},
            "guid": {"type": "string", "description": "电源计划 GUID，从 list 获取；create 时可留空"},
            "name": {"type": "string", "description": "create/rename 时的方案名称"}
        }},
        lambda a: _power_plan_manager(a, confirm), source="system_admin")

    registry.register(
        "scheduled_task_manager",
        "Windows 计划任务增删改查：action=list/detail/create/delete/run/enable/disable。"
        "create 需 name、command、trigger(DAILY/WEEKLY/MONTHLY/HOURLY/ONLOGON/ONSTART)，可选 time(HH:MM)。"
        "所有写操作都会请求确认。",
        {"type": "object", "properties": {
            "action": {"type": "string"},
            "name": {"type": "string", "description": "任务名/路径"},
            "command": {"type": "string", "description": "create 时要执行的命令"},
            "trigger": {"type": "string", "description": "DAILY/WEEKLY/MONTHLY/HOURLY/ONLOGON/ONSTART"},
            "time": {"type": "string", "description": "可选，HH:MM"},
            "keyword": {"type": "string", "description": "list 时筛选"}
        }},
        lambda a: _scheduled_task_manager(a, confirm), source="system_admin")

    registry.register(
        "env_var_manager",
        "环境变量增删改查：action=list/get/set/delete，scope=user/machine。所有值中的疑似凭据会脱敏；"
        "写操作需确认；machine 范围需要管理员。",
        {"type": "object", "properties": {
            "action": {"type": "string", "description": "list/get/set/delete"},
            "scope": {"type": "string", "description": "user 或 machine，默认 user"},
            "name": {"type": "string"},
            "value": {"type": "string"}
        }},
        lambda a: _env_var_manager(a, confirm), source="system_admin")

    registry.register(
        "registry_manager",
        "注册表受控增删改查：action=list/get/set/delete，path 只允许 HKCU\\Software\\LocalAgent、"
        "HKLM\\Software\\LocalAgent、HKCU\\Control Panel\\Desktop 及其子路径。"
        "这是为了防止把系统关键配置改坏；HKLM 写入需管理员；所有写操作需确认。",
        {"type": "object", "properties": {
            "action": {"type": "string", "description": "list/get/set/delete"},
            "path": {"type": "string", "description": "白名单内的注册表路径"},
            "name": {"type": "string", "description": "值名"},
            "value": {"type": "string", "description": "set 时的值（不会回显）"},
            "type": {"type": "string", "description": "string/dword/qword/expand，默认 string"}
        }},
        lambda a: _registry_manager(a, confirm), source="system_admin")
