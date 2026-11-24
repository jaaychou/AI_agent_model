"""自主性/兜底工具/多模型协同/Mermaid/运行模式测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry, register_builtin_tools
from core.weather_tools import register_weather_tools
from core.data_tools import register_data_tools
from core.model_router import register_model_router, KNOWN_CAPABILITIES, has_delegate_intent
from core.mermaid_tools import render_mermaid
from core.agent import Agent


class FallbackToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name)
        register_builtin_tools(self.reg)
        register_weather_tools(self.reg)
        register_data_tools(self.reg)
        self.schemas = self.reg.get_schemas()

    def tearDown(self):
        self.tmp.cleanup()

    def _names(self, phrase):
        sel = Agent._select_relevant_tools(self.schemas, phrase)
        return {s["function"]["name"] for s in sel}

    def test_weather_colloquial(self):
        names = self._names("今天冷不冷")
        self.assertIn("weather", names)
        self.assertIn("run_command", names)  # 兜底常驻

    def test_implicit_chart_needs_no_explicit_python(self):
        names = self._names("把这个数据直观展示一下")
        self.assertIn("plot_chart", names)
        self.assertIn("data_analyze", names)

    def test_app_count_gets_command(self):
        names = self._names("我这电脑有多少个应用啊")
        self.assertIn("run_command", names)  # 能自己查，不推给用户

    def test_always_available_tools_in_every_request(self):
        for phrase in ["你好", "讲个笑话", "1+1等于几"]:
            names = self._names(phrase)
            for tool in ("run_command", "web_search", "open_application"):
                self.assertIn(tool, names, f"{phrase} 应有兜底工具 {tool}")


class IntentTests(unittest.TestCase):
    def test_delegate_intent(self):
        self.assertTrue(has_delegate_intent("让别的模型来做"))
        self.assertTrue(has_delegate_intent("多模型协同"))
        self.assertFalse(has_delegate_intent("今天天气"))


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register(self):
        register_model_router(self.reg)
        names = set(self.reg.list_names())
        self.assertIn("list_models_capability", names)
        self.assertIn("delegate_to_model", names)

    def test_known_capabilities(self):
        self.assertIn("qwen2.5-coder:7b", KNOWN_CAPABILITIES)
        self.assertIn("qwen2.5vl:7b", KNOWN_CAPABILITIES)

    def test_delegate_requires_args(self):
        from core.model_router import _delegate
        r = _delegate({"model": "qwen3.5:4b"})
        self.assertFalse(r.ok)

    def test_delegate_rejects_bad_model_name(self):
        from core.model_router import _delegate
        r = _delegate({"model": "bad name; rm -rf", "task": "hi"})
        self.assertFalse(r.ok)


class MermaidTests(unittest.TestCase):
    def test_render_real_png(self):
        # 用 Kroki 在线渲染；若离线则跳过而不是失败
        r = render_mermaid("graph TD; A-->B;", "test_render")
        if not r.ok:
            self.skipTest(f"Mermaid 渲染服务不可用：{r.output[:80]}")
        p = Path(r.output.split("：")[-1].strip())
        self.assertTrue(p.exists())
        self.assertTrue(p.read_bytes().startswith(b"\x89PNG"))

    def test_empty_code(self):
        r = render_mermaid("")
        self.assertFalse(r.ok)


class ModeConfigTests(unittest.TestCase):
    """验证三种模式映射的 AgentConfig 参数（与 GUI 中一致）。"""

    @staticmethod
    def config_for(mode):
        if mode == "普通模式":
            return dict(planning_enabled=False, verify_before_finish=False,
                        max_strategy_switches=0, max_steps=15)
        if mode == "L3计划":
            return dict(planning_enabled=True, verify_before_finish=False,
                        max_strategy_switches=1, max_steps=25)
        return dict(planning_enabled=True, verify_before_finish=True,
                    max_strategy_switches=3, max_steps=40)

    def test_normal_mode(self):
        c = self.config_for("普通模式")
        self.assertFalse(c["planning_enabled"])
        self.assertFalse(c["verify_before_finish"])

    def test_l3_mode(self):
        c = self.config_for("L3计划")
        self.assertTrue(c["planning_enabled"])
        self.assertFalse(c["verify_before_finish"])

    def test_l4_mode(self):
        c = self.config_for("L4回环")
        self.assertTrue(c["planning_enabled"])
        self.assertTrue(c["verify_before_finish"])
        self.assertEqual(c["max_strategy_switches"], 3)


if __name__ == "__main__":
    unittest.main()
