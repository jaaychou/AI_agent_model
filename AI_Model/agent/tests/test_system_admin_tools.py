"""系统配置增删改工具测试：授权门控、白名单、确认钩子、真实命令参数。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry
from core import system_admin_tools as sat
from core.agent import Agent


GUID = "11111111-2222-3333-4444-555555555555"


class SystemAdminRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_tool_always_available(self):
        sat.register_system_admin_tools(self.reg, confirm=lambda m: True, enabled=False)
        names = set(self.reg.list_names())
        self.assertIn("windows_forensics", names)
        self.assertNotIn("power_plan_manager", names)
        self.assertNotIn("registry_manager", names)

    def test_write_tools_require_explicit_enable(self):
        sat.register_system_admin_tools(self.reg, confirm=lambda m: True, enabled=True)
        names = set(self.reg.list_names())
        for n in ("windows_forensics", "power_plan_manager", "scheduled_task_manager",
                  "env_var_manager", "registry_manager"):
            self.assertIn(n, names)

    def test_intent_filter_loads_write_tools(self):
        sat.register_system_admin_tools(self.reg, confirm=lambda m: True, enabled=True)
        schemas = self.reg.get_schemas()
        selected = Agent._select_relevant_tools(schemas, "帮我改一下电源计划，再设置环境变量")
        names = {s.get("function", {}).get("name") for s in selected if isinstance(s, dict)}
        self.assertIn("power_plan_manager", names)
        self.assertIn("env_var_manager", names)
        self.assertTrue(sat.has_system_admin_intent("查一下注册表"))


class SystemAdminBehaviorTests(unittest.TestCase):
    def test_windows_forensics_wake_uses_real_commands(self):
        calls = []

        def fake_run(argv, timeout=30):
            calls.append(argv)
            return 0, f"output:{argv[1] if len(argv) > 1 else ''}", ""

        with mock.patch.object(sat, "_run", side_effect=fake_run):
            result = sat._windows_forensics({"kind": "wake"})
        self.assertTrue(result.ok)
        self.assertTrue(any(c[:2] == ["powercfg", "/lastwake"] for c in calls))
        self.assertTrue(any(c[:3] == ["powercfg", "/devicequery", "wake_armed"] for c in calls))
        self.assertIn("上次唤醒源", result.output)

    def test_power_plan_set_checks_guid_and_confirmation(self):
        denies = []
        with mock.patch.object(sat, "_run", side_effect=[
                (0, f"Existing Power Schemes (* Active)\n-----------------------------------\n{GUID}  Balanced", ""),
                (0, "电源方案已激活", "")]):
            result = sat._power_plan_manager({"action": "set", "guid": GUID},
                                             lambda m: denies.append(m) or True)
        self.assertTrue(result.ok)
        self.assertEqual(denies[0], f"切换到电源计划 {GUID}。是否继续？")

    def test_power_plan_set_rejects_unknown_guid(self):
        other = "99999999-8888-7777-6666-555555555555"
        with mock.patch.object(sat, "_run", return_value=(0, f"{GUID}  Balanced", "")):
            result = sat._power_plan_manager({"action": "set", "guid": other}, lambda m: True)
        self.assertFalse(result.ok)
        self.assertIn("不在本机电源计划列表", result.output)

    def test_power_plan_delete_blocks_active_scheme(self):
        with mock.patch.object(sat, "_run", return_value=(0, f"{GUID}  Balanced *", "")):
            result = sat._power_plan_manager({"action": "delete", "guid": GUID}, lambda m: True)
        self.assertFalse(result.ok)
        self.assertIn("当前正在使用", result.output)

    def test_env_list_masks_suspected_secret(self):
        raw = "API_KEY=abcdefgh\nPATH=C:\\Windows\n"
        with mock.patch.object(sat, "_run", return_value=(0, raw, "")):
            result = sat._env_var_manager({"action": "list", "scope": "user"}, lambda m: True)
        self.assertIn("API_KEY=[已隐藏]", result.output)
        self.assertIn("PATH=C:\\Windows", result.output)

    def test_scheduled_task_create_rejects_bad_trigger(self):
        result = sat._scheduled_task_manager(
            {"action": "create", "name": "AgentTest", "command": "echo hi", "trigger": "; calc"},
            lambda m: True)
        self.assertFalse(result.ok)
        self.assertIn("trigger", result.output)

    def test_scheduled_task_create_uses_argv(self):
        seen = []
        with mock.patch.object(sat, "_run", side_effect=lambda argv, timeout=30: (
                seen.append(argv) or (0, "创建成功", ""))):
            result = sat._scheduled_task_manager(
                {"action": "create", "name": "AgentTest", "command": "echo hi",
                 "trigger": "ONLOGON"}, lambda m: True)
        self.assertTrue(result.ok)
        self.assertEqual(seen[0][:2], ["schtasks", "/Create"])
        self.assertIn("/TN", seen[0])

    def test_registry_rejects_outside_allowlist(self):
        result = sat._registry_manager(
            {"action": "get", "path": r"HKLM\SYSTEM\CurrentControlSet\Services", "name": "ImagePath"},
            lambda m: True)
        self.assertFalse(result.ok)
        self.assertIn("不在允许列表", result.output)

    def test_registry_write_goes_through_confirm(self):
        prompts = []
        seen = []
        with mock.patch.object(sat, "_run", side_effect=lambda argv, timeout=30: (
                seen.append(argv) or (0, "操作完成", ""))):
            result = sat._registry_manager(
                {"action": "set", "path": r"HKCU\Software\LocalAgent", "name": "Theme",
                 "value": "dark", "type": "string"},
                lambda m: prompts.append(m) or True)
        self.assertTrue(result.ok)
        self.assertIn("写入注册表", prompts[0])
        self.assertIn("REG_SZ", seen[0])

    def test_registry_write_denied_when_confirm_false(self):
        with mock.patch.object(sat, "_run") as run:
            result = sat._registry_manager(
                {"action": "delete", "path": r"HKCU\Software\LocalAgent", "name": "Theme"},
                lambda m: False)
        self.assertFalse(result.ok)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
