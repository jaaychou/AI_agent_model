"""L4 自主闭环：任务规划 → 执行 → 自验收 → 失败换路径。

这是从 L3（给目标自己规划）迈向 L4（模糊需求自主拆解执行验证）的关键模块。
普通 Agent 循环是"模型说不调工具了 = 完成"，本模块补上三件事：

1. TaskPlan：把模糊需求拆成有验收标准的计划，进度可见。
2. 验收闸门：模型想结束时，必须对照验收标准核验真实证据，未达标就返回继续执行。
3. 策略切换：工具失败/空转时不再直接停，而是给模型具体替代路径，多次无效才放弃。

设计给小模型（4B/8B）用：结构化、提示明确、不依赖模型自觉。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .tools import ToolResult


@dataclass
class TaskPlan:
    """一次任务的结构化计划。"""
    goal: str
    steps: list[dict] = field(default_factory=list)       # [{"action", "status", "result"}]
    acceptance: list[str] = field(default_factory=list)   # 验收标准（自然语言）
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "goal": self.goal, "steps": self.steps,
            "acceptance": self.acceptance, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskPlan":
        return cls(
            goal=str(data.get("goal", "")),
            steps=list(data.get("steps") or []),
            acceptance=[str(x) for x in (data.get("acceptance") or [])],
            created_at=str(data.get("created_at", "")),
        )

    def render(self) -> str:
        lines = [f"目标：{self.goal}", "计划："]
        for i, step in enumerate(self.steps, 1):
            status = step.get("status", "pending")
            mark = {"done": "[x]", "failed": "[!]", "working": "[~]"}.get(status, "[ ]")
            lines.append(f"  {mark} {i}. {step.get('action', '')}（{status}）")
        if self.acceptance:
            lines.append("验收标准：")
            for i, criterion in enumerate(self.acceptance, 1):
                lines.append(f"  {i}. {criterion}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 策略切换：失败/空转时给模型具体替代路径，而不是直接停止
# ---------------------------------------------------------------------------

# 按工具名给出"换路"建议。覆盖易失败的场景：命令、文件、网络、MCP、生图。
_STRATEGY_HINTS: dict[str, list[str]] = {
    "run_command": [
        "换 shell（CMD ↔ PowerShell），或把长命令拆成多条短命令分别看退出码",
        "先用 list_dir / read_file 确认路径和文件真实存在，再重试命令",
        "命令失败先看 stderr：缺工具就用 search_tools + install_package 装，或换系统自带命令",
    ],
    "execute_command": [
        "换 shell 或拆小命令；确认工作空间路径是否正确",
        "先确认前置条件（文件/目录/依赖）再重试，别重复相同命令",
    ],
    "run_python_file": [
        "先 read_file 看脚本内容，确认参数与依赖；用 sandbox_run 在隔离目录试运行",
        "缺库时用 install_package 装到隔离目录，再重试",
    ],
    "read_file": [
        "用 list_dir / search_files 确认真实文件名与路径",
        "二进制文件改用 document_to_markdown；工作区外文件需用户开启完整本地访问",
    ],
    "web_fetch": [
        "换 URL 或去掉锚点/查询参数；JS 渲染页面改用浏览器工具（Playwright MCP）",
        "先用 web_search 找到可访问的真实 URL，再抓取",
    ],
    "web_search": [
        "换更短更精确的关键词，加年份；换中英文分别搜",
    ],
    "generate_image": [
        "降低分辨率到 512×512、步数降到 8；确认 ComfyUI 服务在线、显存充足",
    ],
}

_GENERIC_HINTS = [
    "换一个能达成同一目标的工具（discover_tools 看全部可用工具）",
    "缩小任务范围，先完成最小可验证的一步，再继续",
    "检查参数是否符合工具 schema，用更小/更具体的值重试",
    "若能力确实缺失：search_tools 找库 → install_package 装 → create_tool 造工具",
]


def build_strategy_prompt(failed_tool: str, reason: str, switch_index: int) -> str:
    """生成给模型的'换路径'提示，具体可执行，不是空泛的'再试一次'。"""
    hints = list(_STRATEGY_HINTS.get(failed_tool, [])) + _GENERIC_HINTS
    # 每次切换给不同建议，避免重复同一条
    picked = [hints[switch_index % len(hints)], hints[(switch_index + 1) % len(hints)]]
    return (
        f"上一次工具「{failed_tool}」没有产生有效进展：{reason}。\n"
        f"不要再用相同参数重复调用。换一条路径，优先试：\n"
        + "\n".join(f"  - {h}" for h in picked)
        + "\n如果这条路径也走不通，如实说明卡在哪里，不要声称已完成。"
    )


def build_verification_prompt(plan: TaskPlan, trace: list[str] | None = None) -> str:
    """完成前验收闸门：要求模型对照验收标准核验真实证据。"""
    lines = [
        "【完成前验收 · 必须执行】",
        "你正要结束本轮任务。在给出最终答复前，先对照下面的验收标准逐条核对真实结果：",
        plan.render(),
    ]
    if trace:
        lines.append("\n本轮已执行的工具证据：\n" + "\n".join(trace[-12:]))
    lines.append(
        "\n规则：\n"
        "1. 只有工具真实返回成功的证据才算达标；预期、推测、'应该可以'都不算。\n"
        "2. 某条不达标就继续用工具补齐（必要时换路径），不要结束。\n"
        "3. 若确实无法达标，最终答复必须明确指出哪一条没做到及原因，不能说'已完成'。\n"
        "4. 验收通过后，简洁报告结果和证据。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 验收判定：从工具证据里做确定性检查（不依赖模型自觉）
# ---------------------------------------------------------------------------

def evidence_satisfies(criterion: str, trace: list[str]) -> bool | None:
    """尝试用工具证据确定性地判断某条验收标准是否达标。

    返回 True=有明确成功证据，False=有明确失败证据，None=无法自动判定（交模型）。
    只做保守判断：宁可返回 None，也不误判为通过。
    """
    if not criterion or not trace:
        return None
    joined = "\n".join(trace)
    # 明确失败证据：任何 FAILED 且与该标准关键词相关
    keywords = [w for w in re.findall(r"[A-Za-z_]{3,}|[\u4e00-\u9fff]{2,}", criterion) if len(w) >= 2][:6]
    related = [line for line in trace if any(w.casefold() in line.casefold() for w in keywords)]
    if related and all("[FAILED]" in line for line in related):
        return False
    # 明确成功证据：相关行有 SUCCESS
    if any("[SUCCESS]" in line for line in related):
        return True
    return None


def register_autonomy_tools(registry):
    """把 plan_task / update_plan / verify_goal 注册成工具，模型可显式驱动闭环。

    计划存放在 registry.current_plan（TaskPlan 或 None），Agent 循环与工具共享同一份状态。
    """

    def get_plan():
        return getattr(registry, "current_plan", None)

    def set_plan(plan):
        registry.current_plan = plan

    def plan_task(args):
        goal = str(args.get("goal", "")).strip()
        steps = args.get("steps") or []
        acceptance = args.get("acceptance") or []
        if not goal:
            return ToolResult(False, "请提供 goal（本次任务的目标）。")
        norm_steps = []
        for item in steps:
            if isinstance(item, str):
                norm_steps.append({"action": item, "status": "pending", "result": ""})
            elif isinstance(item, dict):
                norm_steps.append({
                    "action": str(item.get("action", "")),
                    "status": str(item.get("status", "pending")),
                    "result": str(item.get("result", "")),
                })
        plan = TaskPlan(goal=goal, steps=norm_steps,
                        acceptance=[str(x) for x in acceptance],
                        created_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        set_plan(plan)
        return ToolResult(True, f"任务计划已建立：\n{plan.render()}\n\n接下来按步骤执行；结束前必须对照验收标准核验。")

    def update_plan(args):
        plan = get_plan()
        if not plan:
            return ToolResult(False, "当前没有任务计划。请先用 plan_task 建立计划。")
        index = args.get("step")
        status = str(args.get("status", "")).strip().casefold()
        result = str(args.get("result", ""))
        if status not in {"pending", "working", "done", "failed"}:
            return ToolResult(False, "status 必须是 pending/working/done/failed 之一。")
        if index is None:
            # 无序号：更新全部为给定状态（少用）
            for step in plan.steps:
                step["status"] = status
                if result:
                    step["result"] = result
        else:
            try:
                i = int(index) - 1
            except (TypeError, ValueError):
                return ToolResult(False, "step 必须是步骤序号（从 1 开始）。")
            if not 0 <= i < len(plan.steps):
                return ToolResult(False, f"步骤序号 {index} 超出范围（共 {len(plan.steps)} 步）。")
            plan.steps[i]["status"] = status
            if result:
                plan.steps[i]["result"] = result
        set_plan(plan)
        return ToolResult(True, f"计划已更新：\n{plan.render()}")

    def verify_goal(args):
        plan = get_plan()
        if not plan:
            return ToolResult(False, "当前没有任务计划，无法验收。请先用 plan_task 建立计划。")
        trace = args.get("evidence") or []
        if isinstance(trace, str):
            trace = [trace]
        trace_lines = [str(x) for x in trace]
        if not plan.acceptance:
            return ToolResult(True, "计划没有显式验收标准。请如实报告你实际做了什么、验证了什么，不要声称未验证的成功。")
        results = []
        all_ok = True
        for i, criterion in enumerate(plan.acceptance, 1):
            verdict = evidence_satisfies(criterion, trace_lines)
            if verdict is None:
                results.append(f"  {i}. {criterion} → 无法自动判定，请以真实工具输出为准自行核验")
                all_ok = False
            elif verdict:
                results.append(f"  {i}. {criterion} → 有成功证据")
            else:
                results.append(f"  {i}. {criterion} → 发现失败证据，未达标")
                all_ok = False
        header = "验收结果：全部达标" if all_ok else "验收结果：存在未达标或无法判定项，请继续处理"
        return ToolResult(all_ok, header + "\n" + "\n".join(results))

    registry.register(
        "plan_task", "为复杂/模糊任务建立结构化计划：拆步骤、定验收标准。建立后每一步可 update_plan 更新状态，结束前用 verify_goal 验收。适合多步任务，简单问答无需调用",
        {"type": "object", "properties": {
            "goal": {"type": "string", "description": "任务目标"},
            "steps": {"type": "array", "description": "步骤列表，字符串或 {action,status,result}",
                      "items": {"type": "string"}},
            "acceptance": {"type": "array", "description": "可核验的验收标准列表", "items": {"type": "string"}},
        }, "required": ["goal"]}, plan_task, source="autonomy")
    registry.register(
        "update_plan", "更新任务计划中某一步的状态（pending/working/done/failed）和结果说明",
        {"type": "object", "properties": {
            "step": {"type": "integer", "description": "步骤序号，从 1 开始"},
            "status": {"type": "string", "description": "pending/working/done/failed"},
            "result": {"type": "string", "description": "该步骤的真实结果"},
        }, "required": ["status"]}, update_plan, source="autonomy")
    registry.register(
        "verify_goal", "对照计划的验收标准核验真实工具证据；返回是否全部达标。模型想结束任务前应先调用它，未达标不能声称完成",
        {"type": "object", "properties": {
            "evidence": {"type": "array", "description": "本轮工具执行证据（可选）", "items": {"type": "string"}},
        }, "required": []}, verify_goal, source="autonomy")
