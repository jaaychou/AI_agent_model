"""MCP 客户端：把外部 MCP 服务器的工具接进 Agent。

MCP = Model Context Protocol，Claude Desktop / Cursor 用的那个工具协议。
通过 stdio 跟 MCP server 通信（JSON-RPC 2.0）。
配置文件：agent/mcp_servers.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path


class MCPClient:
    """连接一个 stdio MCP server，暴露它的工具。"""

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict | None = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None

    def start(self):
        import os
        environ = os.environ.copy()
        environ.update(self.env)
        spawn_options = {}
        if os.name == "nt":
            # MCP servers speak over stdio; a console window is unnecessary and
            # steals focus when the GUI starts Node-based servers such as Playwright.
            spawn_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environ, text=True, bufsize=1,
            **spawn_options,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # 初始化握手
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "local-agent", "version": "1.0"},
        })
        self._notify("notifications/initialized", {})
        return self

    def _read_loop(self):
        while self.proc and self.proc.poll() is None:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    break
                msg = json.loads(line)
                mid = msg.get("id")
                if mid:
                    with self._lock:
                        self._pending[str(mid)] = msg
            except (ValueError, OSError):
                continue

    def _request(self, method: str, params: dict, timeout: int = 30) -> dict:
        self._id += 1
        rid = str(self._id)
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if rid in self._pending:
                    return self._pending.pop(rid)
            time.sleep(0.02)
        raise TimeoutError(f"MCP {self.name}: {method} 超时")

    def _notify(self, method: str, params: dict):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def list_tools(self) -> list[dict]:
        resp = self._request("tools/list", {})
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        resp = self._request("tools/call", {"name": name, "arguments": arguments}, timeout=120)
        if "error" in resp:
            return f"MCP 错误: {resp['error']}"
        result = resp.get("result", {})
        parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def load_mcp_servers(config_path: str | Path) -> list[MCPClient]:
    """读取 mcp_servers.json，返回 MCPClient 列表（未启动）。"""
    p = Path(config_path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    clients = []
    for name, cfg in data.get("mcpServers", {}).items():
        env = {}
        for key, value in (cfg.get("env", {}) or {}).items():
            if isinstance(value, str):
                match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
                if match:
                    value = os.environ.get(match.group(1), "")
            env[key] = str(value)
        clients.append(MCPClient(
            name=name,
            command=cfg.get("command", ""),
            args=cfg.get("args", []),
            env=env,
        ))
    return clients


def register_mcp_tools(registry, clients: list[MCPClient]) -> list[str]:
    """把 MCP 工具注册到 Agent 的工具注册表，返回已注册名字。"""
    registered = []
    for client in clients:
        try:
            client.start()
            tools = client.list_tools()
        except Exception as e:
            print(f"[MCP] {client.name} 启动失败: {e}")
            continue
        for tool in tools:
            name = f"mcp_{client.name}_{tool['name']}"
            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"[MCP:{client.name}] {tool.get('description', '')}",
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            # 闭包捕获 client 和原始工具名
            def make_handler(c=client, tname=tool["name"], desc=tool.get("description", "")):
                def handler(args):
                    if not registry.confirm:
                        return _wrap_mcp_result("MCP 工具调用需要用户确认；当前运行方式没有确认回调，已拒绝。", ok=False)
                    summary = json.dumps(args, ensure_ascii=False)[:1200]
                    if not registry.confirm(f"Agent 请求调用 MCP 服务「{c.name}」的工具「{tname}」。\n{desc}\n\n参数：{summary}\n\n是否允许？"):
                        return _wrap_mcp_result("用户拒绝了 MCP 工具调用。", ok=False)
                    return _wrap_mcp_result(c.call_tool(tname, args))
                return handler
            registry._tools[name] = {
                "schema": schema,
                "handler": make_handler(),
                "source": f"mcp:{client.name}",
            }
            registered.append(name)
        print(f"[MCP] {client.name}: 注册 {len(tools)} 个工具")
    return registered


def _wrap_mcp_result(text: str, ok: bool = True):
    from .tools import ToolResult
    return ToolResult(ok, text)
