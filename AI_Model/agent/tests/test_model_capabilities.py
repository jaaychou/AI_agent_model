"""模型工具调用能力结论 + JSON 变体解析测试（基于 2026-11-24 真实 Ollama 实测）。"""
import json
import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.llm import parse_tool_calls


class ModelCapabilityTests(unittest.TestCase):
    def test_standard_tool_calls_qwen35(self):
        msg = {"tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "武汉"}}}], "content": ""}
        calls = parse_tool_calls(msg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["arguments"]["city"], "武汉")

    def test_json_text_tool_call_qwen_coder(self):
        msg = {"content": '{"name": "get_weather", "arguments": {"city": "武汉"}}', "tool_calls": []}
        calls = parse_tool_calls(msg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["arguments"], {"city": "武汉"})

    def test_markdown_fenced_json_deepseek(self):
        msg = {"content": '```json\n{"action": "get_weather", "parameters": {"city": "武汉"}}\n```', "tool_calls": []}
        calls = parse_tool_calls(msg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["arguments"], {"city": "武汉"})

    def test_probe_result_matches_reality(self):
        """探测结果文件里的结论应与实测一致（四模型能力矩阵）。"""
        probe = AGENT_DIR / "probe_result.json"
        if not probe.exists():
            self.skipTest("未运行 probe_model_tools.py")
        data = json.loads(probe.read_text(encoding="utf-8"))
        by_model = {r["model"]: r for r in data["results"]}
        self.assertEqual(by_model["qwen3.5:4b"]["status"], "OK")
        self.assertEqual(by_model["qwen2.5vl:7b"]["status"], "ERROR")
        self.assertIn("does not support tools", by_model["qwen2.5vl:7b"].get("error", ""))


if __name__ == "__main__":
    unittest.main()
