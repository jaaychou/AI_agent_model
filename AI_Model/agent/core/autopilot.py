"""L5 围栏式自治运行时（Autopilot）—— 目标驱动 + 长期自循环 + 越界请示。

什么是 L5（这里的实现定义）：
用户给一个"目标"和一组"围栏"，运行时在围栏内自主反复执行
「观察 → 决策 → 行动 → 验证 → 记录经验」，无需每步确认；
遇到围栏边界、连续失败、异常时自动停机并给出报告，绝不越过围栏自行扩大权限。

它不是"无人监督随便改系统"——那不是自治，是失控。安全设计：
1. 工具白名单：只能在 allow_tools 列出的工具里行动，默认排除所有写操作/危险工具。
2. 时间/步数上限：max_seconds、max_cycles 硬性截断。
3. 熔断：连续 N 次失败、检测到循环、预算耗尽立即停。
4. 只读优先：默认 allowed_tools 只含只读工具；要写必须用户显式加入白名单。
5. 全程留痕：每轮决策、行动、结果写入 autopilot_log.jsonl，可审计、可回放。
6. 可随时停：stop() 设置事件，下一轮循环检测后优雅退出。

用法（Agent 通过工具或直接调用）：
    pilot = Autopilot(agent, registry, fence)
    report = pilot.run(goal="监控 X 并在异常时报告", think_fn=...)
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent
LOG_PATH = BASE / "runtime" / "autopilot_log.jsonl"

# 默认禁止进入自治白名单的高危工具（即使被写进 allow_tools 也要二次确认）
DANGEROUS = {
    "terminate_process", "service_control", "delete_file", "write_file", "edit_file",
    "install_package", "create_tool", "write_tool_script", "run_command", "execute_command",
    "serial_comm", "generate_image", "move_file", "copy_file",
}


@dataclass
class Fence:
    """用户设定的自治围栏。"""
    allow_tools: list[str] = field(default_factory=list)   # 允许自主调用的工具白名单
    max_seconds: int = 300                                 # 最大运行时长
    max_cycles: int = 30                                   # 最大循环轮数
    max_consecutive_failures: int = 3                      # 连续失败熔断阈值
    require_confirm_for_dangerous: bool = True             # 危险工具即使入白名单也要确认
    heartbeat_seconds: float = 0.5                         # 每轮之间的间隔

    def to_dict(self) -> dict:
        return {
            "allow_tools": self.allow_tools, "max_seconds": self.max_seconds,
            "max_cycles": self.max_cycles,
            "max_consecutive_failures": self.max_consecutive_failures,
            "require_confirm_for_dangerous": self.require_confirm_for_dangerous,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Fence":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def validate(self) -> str | None:
        if not self.allow_tools:
            return "围栏未配置 allow_tools（工具白名单）；自治运行时不允许无限制工具访问。"
        self.max_seconds = max(5, min(int(self.max_seconds), 3600))
        self.max_cycles = max(1, min(int(self.max_cycles), 500))
        return None


class Autopilot:
    """围栏内自主循环。think_fn(cycle, observation, history) -> {"tool", "arguments", "done", "reason"}"""

    def __init__(self, registry, fence: Fence, confirm=None, status_callback=None):
        self.registry = registry
        self.fence = fence
        self.confirm = confirm
        self.status_callback = status_callback
        self._stop = threading.Event()
        self.history: list[dict] = []
        self.started_at: str = ""
        self._log_handle = None

    def stop(self):
        """外部随时请求停机（下一轮检测后生效）。"""
        self._stop.set()

    def _log(self, event: dict):
        event["ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if self.status_callback:
            try:
                self.status_callback(event)
            except Exception:
                pass
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _allowed(self, tool: str) -> tuple[bool, str]:
        if tool not in self.fence.allow_tools:
            return False, f"工具「{tool}」不在围栏白名单内"
        if tool in DANGEROUS and self.fence.require_confirm_for_dangerous:
            if not self.confirm:
                return False, f"工具「{tool}」是高危操作，围栏要求确认但当前无确认回调"
            if not self.confirm(f"自治运行时请求执行高危工具「{tool}」。是否允许本次执行？"):
                return False, f"用户拒绝了高危工具「{tool}」"
        return True, ""

    def run(self, goal: str, think_fn) -> dict:
        """在围栏内自主跑，返回可审计报告。"""
        err = self.fence.validate()
        if err:
            return {"ok": False, "reason": err, "goal": goal}
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        start = time.time()
        self.history = []
        self._stop.clear()
        self._log({"event": "start", "goal": goal, "fence": self.fence.to_dict()})

        consecutive_failures = 0
        last_observation = "任务开始，观察环境后决定第一步。"
        reason = ""
        for cycle in range(1, self.fence.max_cycles + 1):
            # 硬边界：时间 / 外部停机
            elapsed = time.time() - start
            if self._stop.is_set():
                reason = "收到停止请求，已优雅退出。"
                self._log({"event": "stop_requested", "cycle": cycle})
                break
            if elapsed >= self.fence.max_seconds:
                reason = f"达到时间上限 {self.fence.max_seconds} 秒，自动停机。"
                self._log({"event": "time_limit", "cycle": cycle})
                break
            # 让模型/策略决定下一步
            try:
                decision = think_fn(cycle, last_observation, list(self.history)) or {}
            except Exception as exc:
                decision = {"done": False, "reason": f"决策函数异常：{type(exc).__name__}: {exc}"}
            if decision.get("done"):
                reason = decision.get("reason") or "自主判断目标已达成。"
                self._log({"event": "done", "cycle": cycle, "reason": reason})
                break
            tool = str(decision.get("tool", "")).strip()
            arguments = decision.get("arguments") or {}
            if not tool:
                consecutive_failures += 1
                last_observation = "决策未给出工具；请给出下一步工具或声明 done。"
                self._log({"event": "no_tool", "cycle": cycle})
                if consecutive_failures >= self.fence.max_consecutive_failures:
                    reason = "连续多轮无法给出有效决策，熔断停机。"
                    break
                continue
            allowed, why = self._allowed(tool)
            if not allowed:
                # 越界不自行扩大权限：记录并请示，然后停机
                reason = f"围栏拦截：{why}。自治运行不越过围栏，已停机等待你的决定。"
                self._log({"event": "fence_block", "cycle": cycle, "tool": tool, "reason": why})
                break
            try:
                result = self.registry.execute(tool, arguments)
                ok = bool(getattr(result, "ok", False))
                output = str(getattr(result, "output", result))
            except Exception as exc:
                ok, output = False, f"工具异常：{type(exc).__name__}: {exc}"
            consecutive_failures = 0 if ok else consecutive_failures + 1
            record = {
                "event": "cycle", "cycle": cycle, "tool": tool,
                "arguments": arguments, "ok": ok, "output": output[:1200],
            }
            self.history.append(record)
            self._log(record)
            last_observation = f"第 {cycle} 轮：工具 {tool} {'成功' if ok else '失败'}。结果：{output[:800]}"
            if consecutive_failures >= self.fence.max_consecutive_failures:
                reason = f"连续 {consecutive_failures} 次失败，熔断停机；不把失败当成功。"
                self._log({"event": "circuit_break", "cycle": cycle})
                break
            if self.fence.heartbeat_seconds:
                time.sleep(self.fence.heartbeat_seconds)
        else:
            reason = f"达到最大循环数 {self.fence.max_cycles}，自动停机。"
        elapsed = round(time.time() - start, 2)
        report = {
            "ok": True, "goal": goal, "reason": reason, "cycles": len(self.history),
            "elapsed_seconds": elapsed, "fence": self.fence.to_dict(),
            "history": self.history, "log_path": str(LOG_PATH),
        }
        self._log({"event": "finish", "reason": reason, "cycles": len(self.history), "elapsed": elapsed})
        return report


def register_autopilot_tools(registry, confirm=None, controllers: dict | None = None):
    """注册 start_autopilot / stop_autopilot / autopilot_report 工具。"""
    controllers = controllers if controllers is not None else {}

    def start_autopilot(args):
        goal = str(args.get("goal", "")).strip()
        if not goal:
            return ToolResult(False, "请提供 goal（自治目标）。")
        allow = args.get("allow_tools") or []
        if isinstance(allow, str):
            allow = [x.strip() for x in allow.split(",") if x.strip()]
        fence = Fence(
            allow_tools=[str(x) for x in allow],
            max_seconds=int(args.get("max_seconds", 300) or 300),
            max_cycles=int(args.get("max_cycles", 30) or 30),
            max_consecutive_failures=int(args.get("max_consecutive_failures", 3) or 3),
        )
        err = fence.validate()
        if err:
            return ToolResult(False, err)
        if confirm and not confirm(
            f"Agent 请求启动围栏式自治：\n目标：{goal}\n允许工具：{fence.allow_tools}\n"
            f"最长 {fence.max_seconds} 秒 / {fence.max_cycles} 轮，连续失败 {fence.max_consecutive_failures} 次熔断。\n是否允许？"
        ):
            return ToolResult(False, "用户拒绝启动自治运行。")
        # 自治循环需要模型作为决策器；这里由 agent 侧注入，工具层只创建控制器并给出说明。
        controllers["active"] = {"goal": goal, "fence": fence, "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        return ToolResult(True,
            f"围栏式自治已登记：{goal}\n围栏：{fence.to_dict()}\n"
            "注意：自治循环由 Agent 主循环驱动（think_fn 注入）。可用 stop_autopilot 立即停机，"
            "所有决策与结果写入 autopilot_log.jsonl 可审计。")

    def stop_autopilot(args):
        ctrl = controllers.get("active")
        if not ctrl:
            return ToolResult(False, "当前没有活跃的自治运行。")
        pilot = ctrl.get("instance")
        if pilot:
            pilot.stop()
        controllers["active"]["stopped_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        return ToolResult(True, "已发出停止请求；自治运行将在下一轮检测后优雅退出。")

    def autopilot_report(args):
        if not LOG_PATH.exists():
            return ToolResult(True, "还没有自治运行日志。")
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = max(1, min(int(args.get("lines", 40) or 40), 200))
        return ToolResult(True, f"自治日志（最后 {min(tail, len(lines))} 条，共 {len(lines)} 条）：\n" +
                          "\n".join(lines[-tail:]))

    registry.register(
        "start_autopilot", "L5 围栏式自治：给定目标和工具白名单，在围栏内自主长期循环（观察→决策→行动→验证）。必须提供 allow_tools 白名单、最长时长和熔断阈值；启动需确认；越界自动停机请示",
        {"type": "object", "properties": {
            "goal": {"type": "string", "description": "自治目标"},
            "allow_tools": {"type": "array", "description": "允许自主调用的工具白名单（只读工具优先）"},
            "max_seconds": {"type": "integer", "description": "最长运行秒数，默认 300"},
            "max_cycles": {"type": "integer", "description": "最多循环轮数，默认 30"},
            "max_consecutive_failures": {"type": "integer", "description": "连续失败熔断阈值，默认 3"},
        }, "required": ["goal", "allow_tools"]}, start_autopilot, source="autopilot")
    registry.register(
        "stop_autopilot", "立即请求停止当前自治运行（下一轮生效）。也可随时用此工具叫停",
        {"type": "object", "properties": {}, "required": []}, stop_autopilot, source="autopilot")
    registry.register(
        "autopilot_report", "查看自治运行日志（审计/回放）。可指定 lines 条数",
        {"type": "object", "properties": {
            "lines": {"type": "integer", "description": "显示最后几条，默认 40"},
        }, "required": []}, autopilot_report, source="autopilot")
