"""底层能力与 L5 围栏自治测试。"""
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry, ToolResult
from core.disk_tools import register_disk_tools
from core.network_observe import register_network_observe_tools
from core.dsp_tools import register_dsp_tools
from core import autopilot as ap
from core.autopilot import Autopilot, Fence, register_autopilot_tools
from core.agent import Agent


class LowLevelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disk_scan_finds_large_files(self):
        (self.ws / "sub").mkdir()
        (self.ws / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        (self.ws / "sub" / "small.txt").write_text("hi")
        reg = ToolRegistry(workspace=str(self.ws))
        register_disk_tools(reg)
        res = reg.execute("disk_scan", {"path": str(self.ws), "min_size_mb": 1})
        self.assertTrue(res.ok, res.output)
        self.assertIn("big.bin", res.output)
        self.assertIn("总大小", res.output)

    def test_disk_scan_rejects_outside_workspace(self):
        reg = ToolRegistry(workspace=str(self.ws))
        register_disk_tools(reg)
        res = reg.execute("disk_scan", {"path": "C:\\Windows"})
        self.assertFalse(res.ok)
        self.assertIn("超出工作空间", res.output)

    def test_disk_duplicates_finds_copies(self):
        payload = b"Y" * (1024 * 1024)
        (self.ws / "a.bin").write_bytes(payload)
        (self.ws / "b.bin").write_bytes(payload)
        reg = ToolRegistry(workspace=str(self.ws))
        register_disk_tools(reg)
        res = reg.execute("disk_duplicates", {"path": str(self.ws), "min_size_mb": 0.5})
        self.assertTrue(res.ok, res.output)
        self.assertIn("重复文件", res.output)

    def test_network_tools_registered_and_readonly(self):
        reg = ToolRegistry(workspace=str(self.ws))
        register_network_observe_tools(reg)
        names = set(reg.list_names())
        self.assertTrue({"wifi_status", "bluetooth_devices", "net_traffic",
                         "net_connections", "dns_cache"} <= names)
        res = reg.execute("net_traffic", {"seconds": 1})
        self.assertTrue(res.ok)
        self.assertIn("非抓包", res.output)

    def test_dsp_spectrum_finds_dominant_frequency(self):
        reg = ToolRegistry(workspace=str(self.ws))
        register_dsp_tools(reg)
        sig = [math.sin(2 * math.pi * 50 * i / 1000) for i in range(1000)]
        res = reg.execute("dsp_spectrum", {"signal": sig, "fs": 1000, "top": 1})
        self.assertTrue(res.ok, res.output)
        self.assertIn("50.0 Hz", res.output)

    def test_dsp_filter_and_stats(self):
        reg = ToolRegistry(workspace=str(self.ws))
        register_dsp_tools(reg)
        sig = [math.sin(2 * math.pi * 50 * i / 1000) for i in range(200)]
        f = reg.execute("dsp_filter", {"signal": sig, "fs": 1000, "kind": "lowpass", "cutoff": 100})
        self.assertTrue(f.ok, f.output)
        s = reg.execute("dsp_stats", {"signal": sig, "fs": 1000})
        self.assertTrue(s.ok)
        self.assertIn("rms", s.output)

    def test_fence_requires_allowlist(self):
        reg = ToolRegistry(workspace=str(self.ws))
        pilot = Autopilot(reg, Fence())
        report = pilot.run("随便做", lambda *_: {"done": False, "tool": "read_file"})
        self.assertFalse(report["ok"])
        self.assertIn("allow_tools", report["reason"])

    def test_autopilot_fence_blocks_dangerous_tool(self):
        reg = ToolRegistry(workspace=str(self.ws))
        reg.register("delete_file", "d", {"type": "object", "properties": {}},
                     lambda _a: ToolResult(True, "deleted"))
        fence = Fence(allow_tools=["read_file"], max_seconds=5, max_cycles=5)
        pilot = Autopilot(reg, fence)
        report = pilot.run("删文件", lambda *_: {"tool": "delete_file", "arguments": {"path": "x"}})
        self.assertTrue(report["ok"])
        self.assertIn("围栏拦截", report["reason"])
        self.assertEqual(report["cycles"], 0)

    def test_autopilot_circuit_break_on_failures(self):
        reg = ToolRegistry(workspace=str(self.ws))
        reg.register("fail", "f", {"type": "object", "properties": {}},
                     lambda _a: ToolResult(False, "失败"))
        fence = Fence(allow_tools=["fail"], max_seconds=5, max_cycles=10,
                      max_consecutive_failures=2, heartbeat_seconds=0)
        pilot = Autopilot(reg, fence)
        report = pilot.run("总失败", lambda *_: {"tool": "fail", "arguments": {}})
        self.assertIn("熔断", report["reason"])
        self.assertEqual(report["cycles"], 2)

    def test_autopilot_stop_request(self):
        reg = ToolRegistry(workspace=str(self.ws))
        reg.register("ok", "o", {"type": "object", "properties": {}},
                     lambda _a: ToolResult(True, "ok"))
        fence = Fence(allow_tools=["ok"], max_seconds=60, max_cycles=50, heartbeat_seconds=0)
        pilot = Autopilot(reg, fence)
        def think(cycle, *_):
            if cycle == 2:
                pilot.stop()
            return {"tool": "ok", "arguments": {}}
        report = pilot.run("跑一会", think)
        self.assertIn("停止请求", report["reason"])

    def test_autopilot_dangerous_needs_confirm(self):
        reg = ToolRegistry(workspace=str(self.ws))
        reg.register("write_file", "w", {"type": "object", "properties": {}},
                     lambda _a: ToolResult(True, "written"))
        fence = Fence(allow_tools=["write_file"], max_seconds=5, max_cycles=5)
        deny = Autopilot(reg, fence, confirm=lambda _m: False)
        report = deny.run("写文件", lambda *_: {"tool": "write_file", "arguments": {}})
        self.assertIn("围栏拦截", report["reason"])

    def test_autopilot_tools_registered(self):
        reg = ToolRegistry(workspace=str(self.ws))
        allow = mock.Mock(return_value=True)
        register_autopilot_tools(reg, confirm=allow)
        names = set(reg.list_names())
        self.assertTrue({"start_autopilot", "stop_autopilot", "autopilot_report"} <= names)
        res = reg.execute("start_autopilot", {"goal": "监控X", "allow_tools": ["disk_usage"],
                                              "max_seconds": 10, "max_cycles": 3})
        self.assertTrue(res.ok, res.output)
        allow.assert_called_once()

    def test_intents_select_lowlevel_tools(self):
        cases = [
            ("哪个文件夹占了最多空间", "disk_scan"),
            ("查一下当前 WiFi 信号强度", "wifi_status"),
            ("对这个信号做频谱分析", "dsp_spectrum"),
            ("启动自治长期监控磁盘", "start_autopilot"),
        ]
        for text, expected in cases:
            schemas = [{"type": "function", "function": {"name": expected, "description": "x"}}]
            selected = Agent._select_relevant_tools(schemas, text)
            self.assertEqual([s["function"]["name"] for s in selected], [expected], text)

    def test_plain_chat_no_lowlevel_tools(self):
        schemas = [{"type": "function", "function": {"name": "disk_scan", "description": "x"}}]
        self.assertEqual(Agent._select_relevant_tools(schemas, "你好"), [])


if __name__ == "__main__":
    unittest.main()
