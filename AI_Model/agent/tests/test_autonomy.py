"""L4 自主闭环测试：任务规划、验收闸门、失败换路径。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry, ToolResult
from core.autonomy import (TaskPlan, register_autonomy_tools, build_strategy_prompt,
                           build_verification_prompt, evidence_satisfies)
from core.agent import Agent, AgentConfig
from core.llm import LLMProvider


class _FakeProvider(LLMProvider):
    """按脚本返回一串回复；记录每次的 messages 长度与内容。"""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages=None, tools=None, model=None, **kwargs):
        self.calls.append({"messages": list(messages or []), "tools": tools})
        reply = self.replies.pop(0) if self.replies else {"content": "完成", "tool_calls": []}
        if isinstance(reply, tuple):
            return {"content": reply[0], "tool_calls": reply[1]}
        if "tool_calls" not in reply:
            reply["tool_calls"] = []
        return reply


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _registry(self, tools=None):
        reg = ToolRegistry(workspace=self.tmp.name)
        register_autonomy_tools(reg)
        if tools:
            for name, result in tools.items():
                reg.register(name, name, {"type": "object", "properties": {}},
                             lambda _a, r=result: ToolResult(r[0], r[1]), source="test")
        return reg

    def test_plan_task_creates_structured_plan(self):
        reg = self._registry()
        res = reg.execute("plan_task", {
            "goal": "做成带图表的PPT",
            "steps": ["读销售数据", "算汇总", "生成图表", "排版存桌面"],
            "acceptance": ["桌面存在该PPT文件", "文件包含至少一张图表"],
        })
        self.assertTrue(res.ok, res.output)
        self.assertIsNotNone(reg.current_plan)
        self.assertEqual(len(reg.current_plan.steps), 4)
        self.assertIn("验收标准", res.output)
        self.assertIn("做成带图表的PPT", reg.current_plan.goal)

    def test_update_plan_marks_step_status(self):
        reg = self._registry()
        reg.execute("plan_task", {"goal": "g", "steps": ["第一步", "第二步"]})
        res = reg.execute("update_plan", {"step": 1, "status": "done", "result": "真实结果"})
        self.assertTrue(res.ok)
        self.assertEqual(reg.current_plan.steps[0]["status"], "done")
        self.assertEqual(reg.current_plan.steps[0]["result"], "真实结果")

    def test_update_plan_rejects_invalid_step(self):
        reg = self._registry()
        reg.execute("plan_task", {"goal": "g", "steps": ["第一步"]})
        res = reg.execute("update_plan", {"step": 9, "status": "done"})
        self.assertFalse(res.ok)
        res = reg.execute("update_plan", {"step": 1, "status": "unknown"})
        self.assertFalse(res.ok)

    def test_verify_goal_detects_success_evidence(self):
        reg = self._registry()
        reg.execute("plan_task", {"goal": "g", "acceptance": ["生成PPT文件"]})
        trace = ["- write_file({}) [SUCCESS]: 已创建 pptx"]
        res = reg.execute("verify_goal", {"evidence": trace})
        self.assertTrue(res.ok, res.output)
        self.assertIn("全部达标", res.output)

    def test_verify_goal_detects_failure_evidence(self):
        reg = self._registry()
        reg.execute("plan_task", {"goal": "g", "acceptance": ["生成PPT文件"]})
        trace = ["- write_file({}) [FAILED]: 权限不足"]
        res = reg.execute("verify_goal", {"evidence": trace})
        self.assertFalse(res.ok)
        self.assertIn("未达标", res.output)

    def test_evidence_satisfies_conservative(self):
        self.assertIsNone(evidence_satisfies("生成PPT", []))
        self.assertIsNone(evidence_satisfies("无关的验收标准", ["- read_file({}) [SUCCESS]: ok"]))

    def test_strategy_prompt_gives_concrete_alternatives(self):
        p = build_strategy_prompt("run_command", "命令退出码 1", 0)
        self.assertIn("run_command", p)
        self.assertIn("换", p)
        self.assertNotIn("再试一次", p)
        p2 = build_strategy_prompt("run_command", "命令退出码 1", 1)
        self.assertNotIn(p.split("优先试：")[1], p2.split("优先试：")[1])

    def test_verification_prompt_contains_plan_and_rules(self):
        plan = TaskPlan(goal="g", steps=[{"action": "做一步", "status": "done"}],
                        acceptance=["文件存在"], created_at="now")
        p = build_verification_prompt(plan, ["- write_file({}) [SUCCESS]: ok"])
        self.assertIn("文件存在", p)
        self.assertIn("完成前验收", p)
        self.assertIn("不达标", p)

    def test_planning_intent_selects_tools(self):
        schemas = [{"type": "function", "function": {"name": n, "description": "x"}}
                   for n in ("plan_task", "update_plan", "verify_goal")]
        selected = Agent._select_relevant_tools(schemas, "帮我把这份数据做成完整PPT，分步做并验证")
        self.assertEqual({s["function"]["name"] for s in selected},
                         {"plan_task", "update_plan", "verify_goal"})

    def test_plain_chat_no_autonomy_tools(self):
        schemas = [{"type": "function", "function": {"name": "plan_task", "description": "x"}}]
        self.assertEqual(Agent._select_relevant_tools(schemas, "你好"), [])

    def test_verification_gate_blocks_first_finish(self):
        """模型在 run 内建计划后，第一次想结束会被验收闸门拦下继续跑。"""
        reg = self._registry()
        plan_call = {"id": "p1", "name": "plan_task",
                     "arguments": {"goal": "g", "acceptance": ["文件存在"], "steps": ["做一步"]}}
        provider = _FakeProvider([
            ("", [plan_call]),
            {"content": "我完成了"},
            {"content": "已核验完成"},
        ])
        agent = Agent(provider, reg, config=AgentConfig(step_delay=0, planning_enabled=True,
                                                        verify_before_finish=True))
        answer = agent.run("帮我做事")
        self.assertEqual(answer, "已核验完成")
        self.assertEqual(len(provider.calls), 3)
        # 第二次调用（想结束时）应被注入验收提示；检查所有轮次里出现过验收提示
        all_content = "\n".join(str(m.get("content", "")) for call in provider.calls for m in call["messages"])
        self.assertIn("完成前验收", all_content)

    def test_verification_gate_skipped_without_plan(self):
        reg = self._registry()
        provider = _FakeProvider([{"content": "直接回答"}])
        agent = Agent(provider, reg, config=AgentConfig(step_delay=0))
        answer = agent.run("你好")
        self.assertEqual(answer, "直接回答")
        self.assertEqual(len(provider.calls), 1)

    def test_strategy_switch_on_repeated_failure(self):
        """工具连续返回相同失败 → 触发策略切换而非直接停止。"""
        reg = self._registry(tools={"run_command": (False, "same failure")})
        provider = _FakeProvider([
            {"content": "", "tool_calls": [{"id": "c1", "name": "run_command",
                                             "arguments": {"cmd": "x"}}]},
            {"content": "", "tool_calls": [{"id": "c2", "name": "run_command",
                                             "arguments": {"cmd": "x"}}]},
            {"content": "", "tool_calls": [{"id": "c3", "name": "run_command",
                                             "arguments": {"cmd": "x"}}]},
            {"content": "换路径后完成"},
        ])
        agent = Agent(provider, reg, config=AgentConfig(step_delay=0, planning_enabled=True,
                                                        max_strategy_switches=2))
        answer = agent.run("跑个命令")
        self.assertEqual(answer, "换路径后完成")
        # 至少一次策略切换提示进入 messages
        all_content = "\n".join(str(m.get("content", "")) for call in provider.calls for m in call["messages"])
        self.assertIn("换一条路径", all_content)


if __name__ == "__main__":
    unittest.main()
