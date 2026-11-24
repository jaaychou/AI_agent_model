"""后端数据能力：虚拟环境、数据图表、数据分析 —— 让 Agent 主动想到用 Python 干活。

工具：
- venv_manager：创建/查看隔离虚拟环境（python -m venv），在隔离环境里装包。
- plot_chart：matplotlib 画图，保存 PNG 并可弹窗预览（callback 交给 GUI）。
- data_analyze：pandas 读取 CSV/Excel/JSON 做统计描述、分组、筛选，返回文本结果。

依赖 matplotlib/pandas 从 agent/runtime/vendor 隔离加载，不污染全局。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent
VENDOR = BASE / "runtime" / "vendor"
VENV_ROOT = BASE / "runtime" / "venvs"
CHART_DIR = BASE / "data" / "charts"
PY = sys.executable

DATA_PATTERNS = re.compile(
    r"画(?:个|张|幅)?图|绘图|图表|折线|柱状|散点|直方图|pie|饼图|可视化|"
    r"数据分析|统计|mean|均值|分组|透视|csv|excel|xlsx|pandas|"
    r"虚拟环境|venv|建个环境|隔离环境", re.I)


def _ensure_vendor():
    if VENDOR.exists() and str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))


def _venv_list(a):
    VENV_ROOT.mkdir(parents=True, exist_ok=True)
    envs = [p.name for p in VENV_ROOT.iterdir() if p.is_dir() and (p / "pyvenv.cfg").exists()]
    return ToolResult(True, "已有虚拟环境：\n" + ("\n".join(f"- {e}" for e in envs) if envs else "（无）")
                      + f"\n目录：{VENV_ROOT}")


def _venv_create(a, confirm):
    name = (a.get("name") or "").strip()
    packages = a.get("packages") or []
    if not name or not re_safe_name(name):
        return ToolResult(False, "需要合法的 name（字母数字下划线短横线）。")
    if isinstance(packages, str):
        packages = [p.strip() for p in packages.split(",") if p.strip()]
    target = VENV_ROOT / name
    if target.exists():
        return ToolResult(False, f"虚拟环境 {name} 已存在。用 venv_manager(action=list) 查看。")
    msg = f"创建隔离虚拟环境 {name}" + (f" 并安装包：{', '.join(packages)}" if packages else "") + "？"
    if not confirm(msg):
        return ToolResult(False, "用户取消了创建虚拟环境。")
    try:
        VENV_ROOT.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([PY, "-m", "venv", str(target)], capture_output=True, text=True,
                              timeout=120, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return ToolResult(False, f"创建 venv 失败：{proc.stderr[:600]}")
        pip = target / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        installed = []
        for pkg in packages[:10]:
            r = subprocess.run([str(pip), "install", pkg, "--disable-pip-version-check"],
                               capture_output=True, text=True, timeout=180,
                               encoding="utf-8", errors="replace")
            installed.append(f"{pkg}: {'OK' if r.returncode == 0 else '失败 ' + r.stderr[:120]}")
        return ToolResult(True, f"已创建虚拟环境：{target}\nPython：{target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')}"
                          + ("\n" + "\n".join(installed) if installed else ""))
    except Exception as exc:
        return ToolResult(False, f"创建失败：{type(exc).__name__}: {exc}")


def re_safe_name(name):
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", name))


def _venv_manager(a, confirm):
    action = (a.get("action") or "list").lower()
    if action == "list":
        return _venv_list(a)
    if action == "create":
        return _venv_create(a, confirm)
    return ToolResult(False, f"未知 action：{action}。可用：list/create。")


def _resolve_data_path(value: str) -> Path:
    """支持 inline CSV 文本或文件路径。"""
    p = Path(value).expanduser()
    if p.exists():
        return p
    return p


def _plot_chart(a, preview_cb=None):
    """matplotlib 画图。支持 data 为 {labels,values} 或 CSV 路径。"""
    _ensure_vendor()
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        # 中文字体回退：Windows 用微软雅黑/黑体，避免中文标题变方块
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception as exc:
        return ToolResult(False, f"matplotlib 不可用：{type(exc).__name__}: {exc}")

    kind = (a.get("kind") or "bar").lower()
    title = a.get("title") or "图表"
    xlabel = a.get("xlabel") or ""
    ylabel = a.get("ylabel") or ""
    labels = a.get("labels") or []
    values = a.get("values") or []
    csv_path = a.get("csv") or ""

    # 从 CSV 读取（简单：第一列标签，第二列数值）
    if csv_path and not labels:
        p = _resolve_data_path(csv_path)
        if not p.exists():
            return ToolResult(False, f"CSV 不存在：{csv_path}")
        try:
            import csv
            with p.open("r", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.reader(f)
                rows = [r for r in reader if len(r) >= 2]
            header = rows[0] if rows else []
            body = rows[1:] if rows else []
            labels = [r[0] for r in body]
            values = [float(r[1]) for r in body if _is_number(r[1])]
            if not xlabel and header:
                xlabel = header[0]
            if not ylabel and len(header) > 1:
                ylabel = header[1]
        except Exception as exc:
            return ToolResult(False, f"读取 CSV 失败：{exc}")
    if not labels or not values or len(labels) != len(values):
        return ToolResult(False, "需要等长的 labels 和 values，或提供含两列的 csv 路径。")

    try:
        values = [float(v) for v in values]
        fig, ax = plt.subplots(figsize=(9, 5))
        if kind in ("line", "折线"):
            ax.plot(labels, values, marker="o")
        elif kind in ("scatter", "散点"):
            ax.scatter(labels, values)
        elif kind in ("pie", "饼图"):
            ax.pie(values, labels=labels, autopct="%1.1f%%")
        elif kind in ("hist", "直方图"):
            ax.hist(values, bins=int(a.get("bins", 10)))
        else:
            ax.bar(labels, values)
            kind = "bar"
        ax.set_title(title)
        if xlabel:
            ax.set_xlabel(xlabel)
        if ylabel:
            ax.set_ylabel(ylabel)
        plt.xticks(rotation=30, ha="right")
        plt.tight_layout()
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in title if c.isalnum() or c in "-_")[:30] or "chart"
        out = CHART_DIR / f"{safe}_{kind}.png"
        fig.savefig(str(out), dpi=130)
        plt.close(fig)
        msg = f"图表已生成：{out}（类型={kind}，{len(values)} 个数据点）"
        if preview_cb:
            try:
                preview_cb(str(out))
                msg += "，已弹窗预览。"
            except Exception as exc:
                msg += f"（预览失败：{exc}）"
        return ToolResult(True, msg)
    except Exception as exc:
        return ToolResult(False, f"绘图失败：{type(exc).__name__}: {exc}")


def _is_number(s):
    try:
        float(s)
        return True
    except Exception:
        return False


def _data_analyze(a):
    _ensure_vendor()
    try:
        import pandas as pd
    except Exception as exc:
        return ToolResult(False, f"pandas 不可用：{type(exc).__name__}: {exc}")
    path = (a.get("path") or "").strip()
    if not path:
        return ToolResult(False, "需要 path（CSV/Excel/JSON 文件路径）。")
    p = _resolve_data_path(path)
    if not p.exists():
        return ToolResult(False, f"文件不存在：{path}")
    try:
        suffix = p.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            df = pd.read_excel(p)
        elif suffix == ".json":
            df = pd.read_json(p)
        else:
            df = pd.read_csv(p, encoding="utf-8-sig")
        lines = [
            f"文件：{p.name}",
            f"形状：{df.shape[0]} 行 × {df.shape[1]} 列",
            f"列：{', '.join(str(c) for c in df.columns)}",
            "",
            "--- 前5行 ---",
            df.head().to_string(),
            "",
            "--- 数值列统计 ---",
            df.describe().to_string(),
        ]
        col = a.get("group_by")
        if col and col in df.columns:
            num_cols = df.select_dtypes(include="number").columns.tolist()
            if num_cols:
                lines += ["", f"--- 按 {col} 分组均值 ---",
                          df.groupby(col)[num_cols].mean().to_string()]
        return ToolResult(True, "\n".join(lines)[:12000])
    except Exception as exc:
        return ToolResult(False, f"分析失败：{type(exc).__name__}: {exc}")


def register_data_tools(registry, confirm=None, preview_cb=None):
    confirm = confirm or (lambda _m: False)
    registry.register(
        "venv_manager",
        "创建或查看隔离 Python 虚拟环境。action=list 查看；create 创建(name, 可选 packages 列表)。"
        "环境在 agent/runtime/venvs，不污染全局 Python。用户要建虚拟环境/隔离环境时用。",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "create"]},
            "name": {"type": "string", "description": "环境名"},
            "packages": {"type": "array", "items": {"type": "string"}, "description": "要装的包列表"}
        }, "required": ["action"]},
        lambda a: _venv_manager(a, confirm), source="data")

    registry.register(
        "plot_chart",
        "用 matplotlib 画数据图表并保存 PNG（可弹窗预览）。用户要画图/图表/可视化时用。"
        "参数 kind(bar/line/scatter/pie/hist)、title、labels、values，或给 csv(两列:标签,数值)自动读取。",
        {"type": "object", "properties": {
            "kind": {"type": "string", "description": "bar/line/scatter/pie/hist 或中文"},
            "title": {"type": "string", "description": "图表标题"},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "X轴标签"},
            "values": {"type": "array", "items": {"type": "number"}, "description": "数值"},
            "xlabel": {"type": "string"}, "ylabel": {"type": "string"},
            "csv": {"type": "string", "description": "可选：CSV 文件路径（含两列）"}
        }},
        lambda a: _plot_chart(a, preview_cb), source="data")

    registry.register(
        "data_analyze",
        "用 pandas 分析 CSV/Excel/JSON：返回行数、列名、前5行、数值统计，可 group_by 分组求均值。"
        "用户要分析数据/看统计/读表格时用。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "数据文件路径"},
            "group_by": {"type": "string", "description": "可选：分组列名"}
        }, "required": ["path"]},
        _data_analyze, source="data")


def has_data_intent(text: str) -> bool:
    return bool(DATA_PATTERNS.search(text))
