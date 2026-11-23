---
name: windows-desktop-operations
description: Diagnose Windows, inspect connected devices, and launch desktop apps such as VS Code using verified local tools.
triggers: Windows, VS Code, vscode, connected devices, external devices, peripherals, operating system
---

# Windows desktop operations

Use the registered local tools; never invent process, device, version, or window results.

## Before acting
- Use `system_info` or `workspace_info` to get the actual Windows version and absolute workspace path. Do not ask the user for details these tools can read.
- To inspect peripherals, call `query_devices`; report the returned names and state that Windows may include virtual devices. Do not guess specific monitor, keyboard, mouse, printer, or USB models.
- To start VS Code, call `open_vscode` (or `open_application` with `name: vscode`). It opens the current workspace by default. For a file/folder, pass its actual workspace path.
- For CMD or PowerShell tasks, call `run_command` / `execute_command`. Explain the exact command and wait for the user confirmation.

## Verify each action
- A process ID means Windows accepted the launch. It does not prove that an application window appeared. Report only the exact result from the tool.
- If launch fails, use the returned executable path and error to diagnose. Check installation locations and process state with real tools; do not repeat an identical command.
- After a successful action, verify the requested result with an appropriate read-only tool before saying it is complete.
- If the selected model cannot call tools, ask the GUI to switch to a tool-capable model or rely on the GUI's automatic tool-model fallback. Never simulate tool output.

## Permissions
- Prefer the current workspace. Access outside it only when the user has enabled full local access.
- Do not claim administrator access unless the process is elevated. Windows UAC must be approved by the user.
