"""天气与隔离沙盒测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry
from core.weather_tools import register_weather_tools, current_weather_by_coords
from core.sandbox import register_sandbox_tools, SANDBOX_ROOT, _safe_rel
from core.agent import Agent


class WeatherSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_weather_by_city_real(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_weather_tools(registry)
        res = registry.execute("weather", {"city": "Wuhan"})
        self.assertTrue(res.ok, res.output)
        self.assertIn("数据来源", res.output)
        self.assertIn("温度", res.output)

    def test_weather_by_coords_real(self):
        text = current_weather_by_coords(30.59, 114.31)
        self.assertIn("Open-Meteo", text)
        self.assertIn("°C", text)

    def test_weather_requires_input_or_fails_honestly(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_weather_tools(registry)
        res = registry.execute("weather", {})
        self.assertFalse(res.ok)
        self.assertIn("城市名", res.output)

    def test_sandbox_runs_python_isolated(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_sandbox_tools(registry)
        res = registry.execute("sandbox_run", {"language": "python", "code": "print(sum(range(1,101)))"})
        self.assertTrue(res.ok, res.output)
        self.assertIn("5050", res.output)
        self.assertIn("退出码：0", res.output)

    def test_sandbox_writes_files_and_cleans_up(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_sandbox_tools(registry)
        res = registry.execute("sandbox_run", {
            "language": "python",
            "files": {"data.txt": "hello"},
            "code": "print(open('data.txt').read())",
        })
        self.assertTrue(res.ok, res.output)
        self.assertIn("hello", res.output)
        # 默认清理：输出里记录的沙盒目录实际应已被删除
        import re
        m = re.search(r"沙盒目录：(.+)", res.output)
        self.assertIsNotNone(m)
        self.assertFalse(Path(m.group(1).strip()).exists(), "沙盒目录应在执行后被清理")

    def test_sandbox_captures_stderr_and_exit_code(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_sandbox_tools(registry)
        res = registry.execute("sandbox_run", {
            "language": "python", "code": "import sys; print('oops', file=sys.stderr); sys.exit(3)",
        })
        # 闭环要求：非0退出必须 ok=False，让模型看到失败并修正重试
        self.assertFalse(res.ok)
        self.assertIn("退出码：3", res.output)
        self.assertIn("oops", res.output)

    def test_sandbox_rejects_path_escape(self):
        self.assertRaises(ValueError, _safe_rel, "../evil.txt")
        self.assertRaises(ValueError, _safe_rel, "/absolute/path")
        self.assertEqual(_safe_rel("sub/data.txt"), Path("sub/data.txt"))

    def test_sandbox_timeout_reports_unknown_side_effects(self):
        registry = ToolRegistry(workspace=self.tmp.name)
        register_sandbox_tools(registry)
        res = registry.execute("sandbox_run", {
            "language": "python", "code": "import time; time.sleep(10)", "timeout": 1,
        })
        self.assertFalse(res.ok)  # 超时也是失败信号
        self.assertIn("超时", res.output)
        self.assertIn("副作用状态未知", res.output)

    def test_weather_intent_selects_tool(self):
        schemas = [{"type": "function", "function": {"name": "weather", "description": "x"}}]
        selected = Agent._select_relevant_tools(schemas, "武汉今天天气怎么样")
        self.assertIn("weather", {s["function"]["name"] for s in selected})

    def test_sandbox_intent_selects_tool(self):
        schemas = [{"type": "function", "function": {"name": "sandbox_run", "description": "x"}}]
        selected = Agent._select_relevant_tools(schemas, "在隔离环境跑一段测试代码")
        self.assertIn("sandbox_run", {s["function"]["name"] for s in selected})

    def test_plain_chat_only_core_tools(self):
        # 新策略：核心工具(weather 等)常驻兜底可见，非核心(sandbox_run)按意图加载
        schemas = [{"type": "function", "function": {"name": "weather", "description": "x"}},
                   {"type": "function", "function": {"name": "sandbox_run", "description": "x"}}]
        names = {s["function"]["name"] for s in Agent._select_relevant_tools(schemas, "你好")}
        self.assertIn("weather", names)      # 兜底核心工具
        self.assertIn("sandbox_run", names)  # 沙盒也常驻：让 Agent 随时能跑代码/实验


if __name__ == "__main__":
    unittest.main()
