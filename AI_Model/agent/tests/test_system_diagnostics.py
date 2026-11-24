"""系统完全体诊断测试：事件日志/关机原因/GPU硬件/配置。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry
from core import system_diagnostics as sd
from core.agent import Agent


class DiagnosticsRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_tools_registered(self):
        sd.register_system_diagnostics(self.reg)
        names = set(self.reg.list_names())
        for n in ("system_shutdown_reason", "event_log_search", "hardware_monitor",
                  "installed_software", "scheduled_tasks", "driver_list",
                  "network_config", "login_history", "system_config_overview"):
            self.assertIn(n, names)

    def test_intent_detection(self):
        self.assertTrue(sd.has_diag_intent("上次为啥关机"))
        self.assertTrue(sd.has_diag_intent("查显存占用"))
        self.assertTrue(sd.has_diag_intent("系统日志里的蓝屏"))
        self.assertFalse(sd.has_diag_intent("今天吃什么"))

    def test_intent_filter(self):
        sd.register_system_diagnostics(self.reg)
        schemas = self.reg.get_schemas()
        sel = Agent._select_relevant_tools(schemas, "上次关机原因是什么")
        names = {s["function"]["name"] for s in sel}
        self.assertIn("system_shutdown_reason", names)


class DiagnosticsBehaviorTests(unittest.TestCase):
    def test_shutdown_reason_has_evidence_sections(self):
        # 用 mock 避免测试依赖真实事件日志
        with mock.patch.object(sd, "_win_events", return_value="TimeCreated: ... Id: 1074"), \
             mock.patch.object(sd, "_ps", side_effect=[
                 "（蓝屏）", "（硬件错误）", "2026/11/23 15:17:17"]):
            r = sd._shutdown_reason({"days": 14, "limit": 10})
        self.assertTrue(r.ok)
        self.assertIn("综合判断", r.output)
        self.assertIn("1074", r.output)

    def test_shutdown_reason_verdict_for_41(self):
        with mock.patch.object(sd, "_win_events", return_value="Id : 41"), \
             mock.patch.object(sd, "_ps", side_effect=["", "", "boot"]):
            r = sd._shutdown_reason({})
        self.assertIn("Kernel-Power 41", r.output)

    def test_event_log_search_validates_ids(self):
        r = sd._event_log_search({"ids": "1074; DROP TABLE"})
        self.assertFalse(r.ok)

    def test_hardware_monitor_nvidia_field(self):
        # 不强制 GPU 存在，但工具应返回结构化文本
        with mock.patch.object(sd.subprocess, "run") as m:
            m.return_value = mock.Mock(stdout="GPU,100,8192,50,60,30\n", stderr="")
            # 只验证函数能跑通
            r = sd._hardware_monitor({})
        self.assertTrue(r.ok)


if __name__ == "__main__":
    unittest.main()
