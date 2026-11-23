"""创建桌面快捷方式（用 venv-webui 的 pywin32 运行）。"""
import os
from pathlib import Path

from win32com.client import Dispatch

BASE = str(Path(__file__).resolve().parent.parent)
ICON = os.path.join(BASE, "agent", "agent.ico")
shell = Dispatch("WScript.Shell")
desktop = shell.SpecialFolders("Desktop")
link_path = os.path.join(desktop, "本地AI Agent.lnk")

shortcut = shell.CreateShortCut(link_path)
shortcut.TargetPath = os.path.join(BASE, "venv-comfy", "Scripts", "pythonw.exe")
shortcut.Arguments = f'"{os.path.join(BASE, "agent", "agent_gui.py")}"'
shortcut.WorkingDirectory = os.path.join(BASE, "agent")
shortcut.IconLocation = ICON + ",0"
shortcut.Description = "本地 AI Agent - 双击打开"
shortcut.save()

print("CREATED:", link_path)
print("EXISTS:", os.path.exists(link_path))
