"""元工具 / 底层控制的自动化测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry, ToolResult, register_builtin_tools
from core.meta_tools import (register_meta_tools, register_dynamic_tools, _static_check_tool_source,
                             ensure_runtime, DYNAMIC_TOOLS, TOOL_INDEX)
from core.system_control import register_system_control_tools, _system_overview
from core.agent import Agent


class MetaToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        # 把元工具的持久化目录重定向到临时目录，避免污染真实 agent 目录
        self._patchers = [
            mock.patch("core.meta_tools.DYNAMIC_TOOLS", self.ws / "dynamic_tools"),
            mock.patch("core.meta_tools.TOOL_INDEX", self.ws / "runtime" / "tool_index.json"),
            mock.patch("core.meta_tools.VENDOR", self.ws / "runtime" / "vendor"),
            mock.patch("core.meta_tools.RUNTIME", self.ws / "runtime"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmp.cleanup()

    def _registry(self, confirm=None):
        registry = ToolRegistry(workspace=str(self.ws), confirm_callback=confirm)
        return registry

    def test_register_meta_tools_exposes_cognition_chain(self):
        registry = self._registry()
        register_meta_tools(registry)
        names = set(registry.list_names())
        self.assertTrue({"discover_tools", "inspect_environment", "search_tools",
                         "install_package", "create_tool", "write_tool_script"} <= names)
        for name in ("discover_tools", "inspect_environment"):
            result = registry.execute(name, {})
            self.assertTrue(result.ok, f"{name} 应执行成功")
            self.assertIsInstance(result.output, str)

    def test_discover_tools_sees_builtin_and_meta(self):
        registry = self._registry()
        register_builtin_tools(registry)
        register_meta_tools(registry)
        result = registry.execute("discover_tools", {})
        self.assertTrue(result.ok)
        self.assertIn("read_file", result.output)
        self.assertIn("create_tool", result.output)
        self.assertIn("meta", result.output)

    def test_install_package_rejects_bad_name_without_pip(self):
        registry = self._registry(confirm=lambda _m: True)
        register_meta_tools(registry, confirm=lambda _m: True)
        result = registry.execute("install_package", {"package": "good; rm -rf /"})
        self.assertFalse(result.ok)
        self.assertIn("不合法", result.output)

    def test_install_package_respects_confirm_denial(self):
        deny = mock.Mock(return_value=False)
        registry = self._registry(confirm=deny)
        register_meta_tools(registry, confirm=deny)
        with mock.patch("core.meta_tools.subprocess.run") as run:
            result = registry.execute("install_package", {"package": "requests"})
        self.assertFalse(result.ok)
        deny.assert_called_once()
        run.assert_not_called()

    def test_create_tool_registers_and_persists(self):
        allow = mock.Mock(return_value=True)
        registry = self._registry(confirm=allow)
        register_meta_tools(registry, confirm=allow)
        code = '''TOOL_META = {"description": "两个数相加", "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}}\n\ndef run(args):\n    return str(int(args["a"]) + int(args["b"]))\n'''
        result = registry.execute("create_tool", {
            "name": "test_adder", "description": "加法工具", "code": code,
        })
        self.assertTrue(result.ok, result.output)
        self.assertIn("test_adder", registry.list_names())
        self.assertEqual(registry.execute("test_adder", {"a": 2, "b": 3}).output, "5")
        allow.assert_called_once()

    def test_create_tool_blocks_dangerous_code(self):
        registry = self._registry(confirm=lambda _m: True)
        register_meta_tools(registry, confirm=lambda _m: True)
        bad_code = 'def run(args):\n    return eval(args["x"])\n'
        result = registry.execute("create_tool", {
            "name": "bad_tool", "description": "危险", "code": bad_code,
        })
        self.assertFalse(result.ok)
        self.assertIn("安全检查", result.output)
        self.assertNotIn("bad_tool", registry.list_names())

    def test_static_check_rejects_subprocess_import(self):
        reason = _static_check_tool_source("import subprocess\ndef run(a):\n    return 'x'\n")
        self.assertIsNotNone(reason)
        self.assertIn("subprocess", reason)
        self.assertIsNone(_static_check_tool_source("def run(a):\n    return sum(a.values())\n"))

    def test_dynamic_tools_persist_across_restart(self):
        registry1 = self._registry(confirm=lambda _m: True)
        register_meta_tools(registry1, confirm=lambda _m: True)
        code = 'def run(args):\n    return "persisted-ok"\n'
        result = registry1.execute("create_tool", {
            "name": "persist_tool", "description": "持久化测试", "code": code,
        })
        self.assertTrue(result.ok, result.output)
        registry2 = self._registry(confirm=lambda _m: False)
        loaded = register_dynamic_tools(registry2)
        self.assertIn("persist_tool", loaded)
        self.assertEqual(registry2.execute("persist_tool", {}).output, "persisted-ok")

    def test_register_system_control_tools(self):
        registry = self._registry()
        register_system_control_tools(registry)
        names = set(registry.list_names())
        self.assertTrue({"system_status", "list_processes", "terminate_process", "list_windows",
                         "list_services", "service_control", "serial_ports", "env_vars",
                         "registry_read"} <= names)
        status = registry.execute("system_status", {})
        self.assertTrue(status.ok)
        self.assertIn("platform", status.output)

    def test_terminate_process_requires_confirm(self):
        deny = mock.Mock(return_value=False)
        registry = self._registry(confirm=deny)
        register_system_control_tools(registry, confirm=deny)
        result = registry.execute("terminate_process", {"name": "notepad.exe"})
        self.assertFalse(result.ok)
        self.assertIn("拒绝", result.output)
        deny.assert_called_once()

    def test_env_vars_hides_secrets(self):
        registry = self._registry()
        register_system_control_tools(registry)
        with mock.patch.dict("os.environ", {"MY_API_TOKEN": "super-secret-value", "MY_APP_MODE": "prod"}):
            result = registry.execute("env_vars", {"keyword": "MY_"})
        self.assertTrue(result.ok)
        self.assertIn("MY_APP_MODE=prod", result.output)
        self.assertNotIn("super-secret-value", result.output)
        self.assertIn("隐藏", result.output)

    def test_meta_intent_selects_tools_into_context(self):
        schemas = [
            {"type": "function", "function": {"name": "discover_tools", "description": "x"}},
            {"type": "function", "function": {"name": "search_tools", "description": "x"}},
            {"type": "function", "function": {"name": "create_tool", "description": "x"}},
            {"type": "function", "function": {"name": "read_file", "description": "x"}},
        ]
        selected = Agent._select_relevant_tools(schemas, "你有什么工具？能不能装个库自己造一个工具")
        names = {s["function"]["name"] for s in selected}
        self.assertIn("discover_tools", names)
        self.assertIn("search_tools", names)
        self.assertIn("create_tool", names)

    def test_system_intent_selects_control_tools(self):
        schemas = [
            {"type": "function", "function": {"name": "terminate_process", "description": "x"}},
            {"type": "function", "function": {"name": "serial_ports", "description": "x"}},
            {"type": "function", "function": {"name": "read_file", "description": "x"}},
        ]
        selected = Agent._select_relevant_tools(schemas, "看看内存和进程，帮我结束一个卡死的进程并查看串口")
        names = {s["function"]["name"] for s in selected}
        self.assertIn("terminate_process", names)
        self.assertIn("serial_ports", names)

    def test_plain_chat_does_not_leak_meta_tools(self):
        schemas = [
            {"type": "function", "function": {"name": "discover_tools", "description": "x"}},
            {"type": "function", "function": {"name": "create_tool", "description": "x"}},
        ]
        selected = Agent._select_relevant_tools(schemas, "卡尔曼滤波是什么？")
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
