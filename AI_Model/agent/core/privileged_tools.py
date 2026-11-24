"""高权限底层工具 —— 固件信息、UEFI 启动项、Win+R 式系统命令、开机自启动。

设计原则（务必先看）：
1. 全部工具受"授权开关"门控：只有当 GUI 对应开关打开时才注册/可用。
2. 写操作一律走 confirm 回调，用户逐次可见可控。
3. 诚实边界 —— 这是当前 Windows 用户的真实权限，不是 OS 沙盒，也不绕过 UAC：
   - 固件/BIOS：Windows 用户态**可以读取** SMBIOS/UEFI 信息、用 bcdedit 管理启动项，
     但**不能也不应该由 Agent 无人值守"刷 BIOS 固件"**。刷写必须厂商官方工具 +
     管理员 + 用户手动确认，刷坏会变砖。本模块只做只读查询 + 启动项管理，
     并提供 manufacturer 指引，绝不执行固件写入。
   - Win+R 命令：用 ShellExecute/start 触发，等价于用户按 Win+R 输入，按当前权限运行。
   - 开机自启动：管理"启动"文件夹与 HKCU/HKLM Run 注册表项。HKLM 需管理员。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .tools import ToolResult

IS_WINDOWS = os.name == "nt"

FIRMWARE_PATTERNS = re.compile(
    r"bios|固件|firmware|uefi|启动项|bcdedit|secure boot|安全启动|主板|smbios", re.I)
WINR_PATTERNS = re.compile(
    r"win\+r|运行命令|shell命令|打开注册表|regedit|msconfig|services\.msc|"
    r"执行系统命令|运行.*(?:程序|命令)|shell.*execute", re.I)
STARTUP_PATTERNS = re.compile(
    r"开机自启|开机启动|自启动|启动项|autorun|开机自动|登录时启动|随.*启动", re.I)


def _ps(script: str, timeout: int = 30):
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def _firmware_info(args):
    """只读读取 SMBIOS/BIOS 与 UEFI 环境信息。"""
    if not IS_WINDOWS:
        return ToolResult(False, "固件信息查询仅支持 Windows。")
    script = (
        "Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,"
        "Version,ReleaseDate,SerialNumber | Format-List | Out-String -Width 200; "
        "'---BOARD---'; "
        "Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product,Version | "
        "Format-List | Out-String -Width 200; "
        "'---SYSTEM---'; "
        "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | "
        "Format-List | Out-String -Width 200; "
        "'---FIRMWARE_TYPE---'; "
        "(Get-CimInstance Win32_ComputerSystem).PCSystemTypeEX; "
        "try { 'SecureBoot=' + (Confirm-SecureBootUEFI) } catch { 'SecureBoot=未知/传统BIOS' }"
    )
    try:
        proc = _ps(script)
        out = (proc.stdout or "").strip()
        if not out:
            return ToolResult(False, f"读取固件信息失败：{proc.stderr[:500]}")
        return ToolResult(True, out[:6000])
    except Exception as exc:
        return ToolResult(False, f"读取固件信息失败：{type(exc).__name__}: {exc}")


def _uefi_boot_entries(args):
    """只读查看 UEFI 启动项与当前启动配置（bcdedit /enum）。"""
    if not IS_WINDOWS:
        return ToolResult(False, "UEFI 启动项查询仅支持 Windows。")
    try:
        proc = subprocess.run(["bcdedit", "/enum"], capture_output=True, text=True,
                              timeout=20, encoding="utf-8", errors="replace")
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        note = ("\n\n说明：以上是启动配置（BCD）的真实内容，只读。修改启动项需管理员权限且有误导系统无法启动的风险，"
                "本工具不会自动修改。如需变更请告知我具体目标，我会给出需你手动确认的命令。")
        return ToolResult(True, (out[:8000] or "无启动项信息") + note)
    except Exception as exc:
        return ToolResult(False, f"读取启动项失败：{type(exc).__name__}: {exc}")


def _bios_update_guidance(args):
    """给出厂商 BIOS 更新的安全指引（诚实说明 Agent 不能无人值守刷写）。"""
    script = ("Get-CimInstance Win32_BIOS | Select-Object -ExpandProperty Manufacturer; "
              "Get-CimInstance Win32_ComputerSystem | Select-Object -ExpandProperty Model")
    maker = "未知"
    model = "未知"
    try:
        proc = _ps(script)
        lines = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
        if lines:
            maker = lines[0]
        if len(lines) > 1:
            model = lines[1]
    except Exception:
        pass
    guidance = (
        f"检测到主板厂商：{maker}，机型：{model}。\n\n"
        "关于『刷 BIOS / 修改固件』的诚实说明：\n"
        "1. Windows 用户态程序（包括本 Agent）无法安全、可靠地直接写入 BIOS/UEFI 固件芯片。"
        "真正的固件刷写必须由主板/整机厂商的官方工具，在管理员权限下由**你本人确认**完成。\n"
        "2. 刷写过程中断电或镜像不匹配会导致主板变砖，无法由软件回滚，因此绝不能无人值守自动执行。\n"
        "3. 我能做的：帮你查当前 BIOS 版本(firmware_info)、查厂商官网最新固件版本、下载官方更新工具、"
        "生成一步步操作清单，每一步都由你手动点击确认。\n"
        "4. 我能辅助的相关能力：UEFI 启动项只读查看(uefi_boot_entries)、Secure Boot 状态查询、"
        "以及通过 bcdedit 的启动配置变更（需管理员，且我会先给你命令让你确认）。\n\n"
        f"建议下一步：到 {maker} 官网支持页，搜索型号 {model} 的 BIOS 更新，核对版本号后再决定是否升级。"
    )
    return ToolResult(True, guidance)


def _run_windows_command(args, confirm):
    """Win+R 式系统命令：ShellExecute 打开程序/控制面板/运行对话框命令。"""
    command = (args.get("command") or "").strip()
    if not command:
        return ToolResult(False, "缺少 command 参数。例如 msconfig、regedit、services.msc、notepad、calc。")
    # 只允许看起来像 Win+R 命令的短命令，避免被当成任意 powershell 注入
    if len(command) > 300:
        return ToolResult(False, "command 过长，Win+R 命令通常很短，请检查。")
    if not confirm(f"执行 Win+R 式系统命令：{command}\n将以当前用户权限运行（不是管理员提权）。是否继续？"):
        return ToolResult(False, "用户拒绝了执行。")
    if not IS_WINDOWS:
        return ToolResult(False, "Win+R 命令仅支持 Windows。")
    try:
        if os.startfile:  # type: ignore[attr-defined]
            os.startfile(command)  # Windows 关联打开，等价于 Win+R
            return ToolResult(True, f"已向 Windows 提交运行请求：{command}。请查看是否弹出对应窗口。")
    except Exception as exc:
        # 回退到 shell
        try:
            subprocess.Popen(command, shell=True)
            return ToolResult(True, f"已通过 shell 提交命令：{command}（{exc}）。")
        except Exception as exc2:
            return ToolResult(False, f"执行命令失败：{exc}；回退也失败：{exc2}")
    return ToolResult(False, "当前平台不支持 os.startfile。")


def _startup_list(args):
    """列出开机自启动项：启动文件夹 + HKCU/HKLM Run 注册表。"""
    if not IS_WINDOWS:
        return ToolResult(False, "开机自启动管理仅支持 Windows。")
    parts = []
    script = (
        "'=== HKCU Run ==='; "
        "Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
        "| Select-Object * -ExcludeProperty PS* | Format-List | Out-String -Width 200; "
        "'=== HKLM Run ==='; "
        "Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run' -ErrorAction SilentlyContinue "
        "| Select-Object * -ExcludeProperty PS* | Format-List | Out-String -Width 200; "
        "'=== Startup Folder ==='; "
        "Get-ChildItem \"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\" "
        "-ErrorAction SilentlyContinue | Select-Object Name,FullName | Format-Table -AutoSize | Out-String -Width 200"
    )
    try:
        proc = _ps(script)
        out = (proc.stdout or "").strip()
        return ToolResult(True, out[:8000] or "未发现自启动项")
    except Exception as exc:
        return ToolResult(False, f"读取自启动项失败：{type(exc).__name__}: {exc}")


def _startup_add(args, confirm):
    """添加一个开机自启动程序（HKCU Run，无需管理员）。"""
    name = (args.get("name") or "").strip()
    command = (args.get("command") or "").strip()
    target = (args.get("target") or "user").lower()  # user / system
    if not name or not command:
        return ToolResult(False, "需要 name（自启动项名称）和 command（要运行的命令/程序路径）。")
    if not confirm(f"添加开机自启动项：\n名称：{name}\n命令：{command}\n范围：{target}\n开机时会自动运行。是否继续？"):
        return ToolResult(False, "用户取消了添加自启动项。")
    if not IS_WINDOWS:
        return ToolResult(False, "仅支持 Windows。")
    key = ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" if target != "system"
           else "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run")
    ps_cmd = (f"New-ItemProperty -Path '{key}' -Name '{name}' -Value '{command}' "
              f"-PropertyType String -Force | Out-Null; "
              f"(Get-ItemProperty -Path '{key}' -Name '{name}').{name}")
    try:
        proc = _ps(ps_cmd)
        if proc.returncode != 0 or ("拒绝访问" in proc.stderr or "Access" in proc.stderr):
            return ToolResult(False, f"写入失败（HKLM 需管理员启动 Agent）：{proc.stderr[:400]}")
        return ToolResult(True, f"已添加自启动项 {name} = {command}（{key}）。重启后生效。")
    except Exception as exc:
        return ToolResult(False, f"添加失败：{type(exc).__name__}: {exc}")


def _startup_remove(args, confirm):
    """删除一个开机自启动项。"""
    name = (args.get("name") or "").strip()
    target = (args.get("target") or "user").lower()
    if not name:
        return ToolResult(False, "需要 name（要删除的自启动项名称）。先用 startup_manager(action=list) 查看。")
    if not confirm(f"删除开机自启动项：{name}（{target}）。删除后不再随开机运行。是否继续？"):
        return ToolResult(False, "用户取消了删除。")
    if not IS_WINDOWS:
        return ToolResult(False, "仅支持 Windows。")
    key = ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" if target != "system"
           else "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run")
    ps_cmd = f"Remove-ItemProperty -Path '{key}' -Name '{name}' -ErrorAction Stop; 'removed'"
    try:
        proc = _ps(ps_cmd)
        if proc.returncode != 0:
            return ToolResult(False, f"删除失败（可能项不存在或权限不足）：{proc.stderr[:400]}")
        return ToolResult(True, f"已删除自启动项 {name}。")
    except Exception as exc:
        return ToolResult(False, f"删除失败：{type(exc).__name__}: {exc}")


def register_privileged_tools(registry, confirm=None, groups=None):
    """按授权分组注册高权限工具。groups 是集合，可含 'firmware'/'startup'/'winr'。
    未包含的分组一个都不注册（GUI 对应授权开关关闭时传空集合）。"""
    groups = set(groups or [])
    if not groups:
        return
    confirm = confirm or (lambda _m: False)

    if "firmware" in groups:
        registry.register(
            "firmware_info",
            "只读读取本机 BIOS/UEFI 固件信息：厂商、版本、发布日期、主板型号、Secure Boot 状态。"
            "用户问 BIOS 版本、固件信息、安全启动状态时使用。",
            {"type": "object", "properties": {}},
            _firmware_info, source="privileged")

        registry.register(
            "uefi_boot_entries",
            "只读查看 UEFI/BIOS 启动项与启动配置（bcdedit /enum）。用户问启动项、启动顺序时使用；不会自动修改。",
            {"type": "object", "properties": {}},
            _uefi_boot_entries, source="privileged")

        registry.register(
            "bios_update_guidance",
            "查询主板/机型并给出安全的 BIOS 固件升级指引。用户说要『改 BIOS』『刷 BIOS』『更新固件』时使用——"
            "诚实说明 Agent 不能无人值守刷固件，给出厂商官方工具与手动步骤。",
            {"type": "object", "properties": {}},
            _bios_update_guidance, source="privileged")

    if "winr" in groups:
        registry.register(
            "run_windows_command",
            "执行 Win+R 式系统命令（等价用户按 Win+R 输入回车），如 msconfig、regedit、services.msc、"
            "notepad、calc、control。按当前用户权限运行，每次需用户确认。",
            {"type": "object", "properties": {
                "command": {"type": "string", "description": "Win+R 命令，如 services.msc"}
            }, "required": ["command"]},
            lambda a: _run_windows_command(a, confirm), source="privileged")

    def startup_manager(a):
        action = (a.get("action") or "list").lower()
        if action == "list":
            return _startup_list(a)
        if action == "add":
            return _startup_add(a, confirm)
        if action == "remove":
            return _startup_remove(a, confirm)
        return ToolResult(False, f"未知 action：{action}。可用：list/add/remove。")

    if "startup" in groups:
        registry.register(
            "startup_manager",
            "管理开机自启动程序：action=list 查看；add 添加(name/command/target)；remove 删除(name/target)。"
            "target 为 user(默认,无需管理员)或 system(需管理员)。增删都需用户确认。",
            {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"], "description": "操作类型"},
                "name": {"type": "string", "description": "自启动项名称(add/remove 时必填)"},
                "command": {"type": "string", "description": "要运行的命令(add 时必填)"},
                "target": {"type": "string", "enum": ["user", "system"], "description": "user=HKCU 无需管理员；system=HKLM 需管理员"}
            }, "required": ["action"]},
            startup_manager, source="privileged")


def select_privileged_tools(text: str) -> bool:
    """是否有固件/Win+R/自启动意图（用于按需筛选工具 schema）。"""
    return bool(FIRMWARE_PATTERNS.search(text) or WINR_PATTERNS.search(text)
                or STARTUP_PATTERNS.search(text))
