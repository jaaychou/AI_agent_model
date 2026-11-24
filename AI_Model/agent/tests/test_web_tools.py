"""网页抓取与串口读写的测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry
from core.web_tools import register_web_tools, _is_blocked_url, _Extractor
from core.system_control import register_system_control_tools
from core.agent import Agent


class WebToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ssrf_blocks_private_and_non_http(self):
        self.assertIn("只支持", _is_blocked_url("file:///C:/Windows/win.ini"))
        self.assertIn("内网", _is_blocked_url("http://127.0.0.1:8080/admin"))
        self.assertIn("内网", _is_blocked_url("http://192.168.1.1/"))
        self.assertIn("内网", _is_blocked_url("http://10.0.0.1/"))
        self.assertIsNone(_is_blocked_url("https://example.com/page"))

    def test_extractor_pulls_text_and_links(self):
        parser = _Extractor()
        parser.feed('<html><body><h1>标题</h1><p>正文内容</p>'
                    '<script>var x=1;</script><a href="/next.html">下一页</a></body></html>')
        text = "".join(parser.text_parts)
        self.assertIn("标题", text)
        self.assertIn("正文内容", text)
        self.assertNotIn("var x", text)
        self.assertTrue(any(href == "/next.html" for _label, href in parser.links))

    def test_web_fetch_uses_real_page(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_web_tools(registry)
        with mock.patch("core.web_tools._robots_allows", return_value=True):
            result = registry.execute("web_fetch", {"url": "https://example.com/", "max_chars": 500})
        self.assertTrue(result.ok, result.output)
        self.assertIn("example.com", result.output)

    def test_web_fetch_reports_failure_honestly(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_web_tools(registry)
        result = registry.execute("web_fetch", {"url": "http://127.0.0.1:9/"})
        self.assertFalse(result.ok)
        self.assertIn("拒绝", result.output)

    def test_serial_comm_requires_pyserial_and_confirms_writes(self):
        allow = mock.Mock(return_value=True)
        registry = ToolRegistry(workspace=self.tmp.name, confirm_callback=allow)
        register_system_control_tools(registry, confirm=allow)
        with mock.patch.dict(sys.modules, {"serial": None}):
            res = registry.execute("serial_comm", {"action": "read", "port": "COM3"})
        self.assertFalse(res.ok)
        self.assertIn("pyserial", res.output)

    def test_serial_list_works(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_system_control_tools(registry)
        res = registry.execute("serial_comm", {"action": "list"})
        self.assertTrue(res.ok)

    def test_fetch_intent_selects_web_fetch(self):
        schemas = [{"type": "function", "function": {"name": "web_fetch", "description": "x"}},
                   {"type": "function", "function": {"name": "read_file", "description": "x"}}]
        selected = Agent._select_relevant_tools(schemas, "抓取这个网页 https://example.com 的正文")
        self.assertIn("web_fetch", {s["function"]["name"] for s in selected})

    def test_serial_intent_selects_serial_comm(self):
        schemas = [{"type": "function", "function": {"name": "serial_comm", "description": "x"}}]
        selected = Agent._select_relevant_tools(schemas, "用串口 COM3 读传感器数据")
        self.assertIn("serial_comm", {s["function"]["name"] for s in selected})

    def test_plain_chat_no_web_fetch(self):
        schemas = [{"type": "function", "function": {"name": "web_fetch", "description": "x"}}]
        self.assertEqual(Agent._select_relevant_tools(schemas, "你好"), [])


if __name__ == "__main__":
    unittest.main()
