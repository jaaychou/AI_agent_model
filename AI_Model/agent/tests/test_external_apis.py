"""外部 API（地图/车票）无 key 时的诚实降级测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry
from core import external_apis
from core.external_apis import register_external_api_tools
from core.agent import Agent


class ExternalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_key_reports_application_guide(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_external_api_tools(registry)
        for name, args in [("geocode", {"address": "武汉"}),
                           ("route_plan", {"origin": "a", "destination": "b"}),
                           ("search_place", {"keyword": "景点"}),
                           ("ticket_query", {"kind": "train"})]:
            res = registry.execute(name, args)
            self.assertFalse(res.ok, f"{name} 无 key 应失败")
            self.assertIn("未配置", res.output)
        geo = registry.execute("geocode", {"address": "武汉"})
        self.assertIn("lbs.qq.com", geo.output)

    def test_ticket_no_fabrication(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_external_api_tools(registry)
        res = registry.execute("ticket_query", {"kind": "flight"})
        self.assertFalse(res.ok)
        self.assertIn("官方购票页", res.output)

    def test_key_loaded_from_config(self):
        with mock.patch.object(external_apis, "KEYS_FILE", Path(self.tmp.name) / "api_keys.json"):
            (Path(self.tmp.name) / "api_keys.json").write_text(
                json.dumps({"tencent_map_key": "TEST_KEY_123"}), encoding="utf-8")
            self.assertEqual(external_apis._key("tencent_map_key"), "TEST_KEY_123")
            self.assertEqual(external_apis._key("train_ticket_api"), "")

    def test_geocode_uses_tencent_when_key_present(self):
        with mock.patch.object(external_apis, "KEYS_FILE", Path(self.tmp.name) / "api_keys.json"):
            (Path(self.tmp.name) / "api_keys.json").write_text(
                json.dumps({"tencent_map_key": "K"}), encoding="utf-8")
            registry = ToolRegistry(workspace=self.tmp.name)
            register_external_api_tools(registry)
            fake = {"status": 0, "result": {"location": {"lat": 30.59, "lng": 114.31}, "address": "湖北省武汉市"}}
            with mock.patch("core.external_apis._tmap_get", return_value=fake):
                res = registry.execute("geocode", {"address": "武汉"})
            self.assertTrue(res.ok, res.output)
            self.assertIn("30.59", res.output)
            self.assertIn("腾讯", res.output)

    def test_map_intent_selects_tools(self):
        schemas = [{"type": "function", "function": {"name": "geocode", "description": "x"}},
                   {"type": "function", "function": {"name": "route_plan", "description": "x"}}]
        selected = Agent._select_relevant_tools(schemas, "帮我查景点的地图坐标和驾车路线")
        self.assertIn("geocode", {s["function"]["name"] for s in selected})

    def test_ticket_intent_selects_tool(self):
        schemas = [{"type": "function", "function": {"name": "ticket_query", "description": "x"}}]
        selected = Agent._select_relevant_tools(schemas, "查一下去武汉的高铁票票价")
        self.assertIn("ticket_query", {s["function"]["name"] for s in selected})

    def test_plain_chat_no_external_tools(self):
        schemas = [{"type": "function", "function": {"name": "geocode", "description": "x"}}]
        self.assertEqual(Agent._select_relevant_tools(schemas, "写一段 Python"), [])

    def test_example_config_has_all_slots(self):
        example = AGENT_DIR / "api_keys.example.json"
        data = json.loads(example.read_text(encoding="utf-8"))
        for key in ("tencent_map_key", "train_ticket_api", "flight_api"):
            self.assertIn(key, data)
            self.assertEqual(data[key], "")


if __name__ == "__main__":
    unittest.main()
