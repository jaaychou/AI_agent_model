"""Skill 加载器：让 Agent 具备可插拔的"专业技能"。

Skill 就是一个文件夹，里面有 SKILL.md（YAML frontmatter + Markdown 说明）。
Agent 启动时加载所有 skill 的简介到系统提示，需要时读取完整内容。
这就是 WorkBuddy / Claude Code 的 Skill 机制。
"""
from __future__ import annotations

import re
from pathlib import Path


def parse_skill_file(path: Path) -> dict:
    """解析 SKILL.md 的 YAML frontmatter。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = {}
    body = text
    m = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$", text)
    if m:
        front, body = m.group(1), m.group(2)
        for line in front.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
    return {
        "name": meta.get("name", path.parent.name),
        "description": meta.get("description", ""),
        "path": str(path),
        "dir": str(path.parent),
        "content": body.strip(),
        "triggers": [t.strip() for t in meta.get("triggers", "").split(",") if t.strip()],
    }


def load_skills(skills_dir: str | Path) -> list[dict]:
    """扫描 skills 目录下所有 SKILL.md。"""
    root = Path(skills_dir)
    if not root.exists():
        return []
    skills = []
    for p in sorted(root.rglob("SKILL.md")):
        try:
            skills.append(parse_skill_file(p))
        except Exception as e:
            print(f"[Skill] 跳过 {p}: {e}")
    return skills


def build_skills_prompt(skills: list[dict]) -> str:
    """生成给模型看的 skill 简介（注入系统提示）。"""
    if not skills:
        return ""
    lines = ["\n\n## 可用技能（Skills）", "需要时调用 read_skill 工具读取完整说明，然后按说明执行。"]
    for s in skills:
        lines.append(f"- **{s['name']}**: {s['description']}")
    return "\n".join(lines)


def register_skill_tools(registry, skills: list[dict]):
    """注册 read_skill 和 list_skills 工具。"""
    from .tools import ToolResult

    def list_skills(args):
        text = "\n".join(f"- {s['name']}: {s['description']}" for s in skills)
        return ToolResult(True, f"可用技能：\n{text}" if text else "当前没有已加载的技能")

    def read_skill(args):
        name = args.get("name", "")
        for s in skills:
            if s["name"] == name or name in s["triggers"]:
                return ToolResult(True, f"# 技能：{s['name']}\n\n{s['content']}")
        return ToolResult(False, f"找不到技能 '{name}'。可用：{[s['name'] for s in skills]}")

    registry.register(
        "list_skills", "列出所有可用的专业技能",
        {"type": "object", "properties": {}, "required": []},
        list_skills,
    )
    registry.register(
        "read_skill", "读取某个技能的完整说明，按说明执行任务",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名称"}
        }, "required": ["name"]},
        read_skill,
    )
