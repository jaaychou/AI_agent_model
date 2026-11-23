#!/usr/bin/env python3
"""本地 Agent 桌面版 —— Codex 风格的简约界面。

双击打开：白色简约、居中欢迎语、底部悬浮输入框。
自动启动 Ollama，选模型、选工作空间、输入任务即可。
"""
from __future__ import annotations

import json
import os
import re
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

AI_HOME = BASE.parent
OLLAMA_EXE = AI_HOME / "Ollama" / "ollama.exe"
OLLAMA_MODELS = AI_HOME / "models"
CONFIG_PATH = BASE / "config.json"
MCP_PATH = BASE / "mcp_servers.json"
SKILLS_DIR = BASE / "skills"

C_BG = "#ffffff"
C_BG_SOFT = "#f7f8fa"
C_BORDER = "#e3e5e8"
C_TEXT = "#1c1e21"
C_TEXT_DIM = "#8a8f98"
C_BLUE = "#3b7cff"
C_BLUE_DARK = "#2f66d6"
C_USER_BUBBLE = "#e9f0ff"
FONT_UI = "Microsoft YaHei UI"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TEXT_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".csv", ".log"}


def bind_chat_shortcuts(widget, send_callback):
    """Enter sends; Shift+Enter inserts a newline in the chat input."""
    def send(_event=None):
        send_callback()
        return "break"

    def newline(event):
        event.widget.insert("insert", "\n")
        return "break"

    widget.bind("<Return>", send)
    widget.bind("<Control-Return>", send)
    widget.bind("<Shift-Return>", newline)


def add_hover_effect(widget, normal_bg, hover_bg):
    widget.configure(bg=normal_bg, activebackground=hover_bg)
    widget.bind("<Enter>", lambda _event: widget.configure(bg=hover_bg))
    widget.bind("<Leave>", lambda _event: widget.configure(bg=normal_bg))


def responsive_input_margin(width: int) -> int:
    target_width = min(int(width * 0.90), 1680)
    return max(12, (width - target_width) // 2)


def choose_tool_model(available_models, capabilities_by_model, needs_vision=False):
    known = {
        "qwen2.5vl:7b": ["vision"],
        "qwen2.5-coder:7b": ["tools"],
        "deepseek-r1:8b": ["tools", "thinking"],
        "qwen3.5:4b": ["tools", "vision", "thinking"],
    }
    for name in ("qwen3.5:4b", "deepseek-r1:8b", "qwen2.5-coder:7b"):
        if name not in available_models:
            continue
        capabilities = capabilities_by_model.get(name, known.get(name, []))
        if "tools" in capabilities and (not needs_vision or "vision" in capabilities):
            return name
    return None


def prepare_attachments(paths: list[str], prompt: str, workspace: str | None = None) -> tuple[str, list[str]]:
    """Read small source/text attachments and return vision image paths separately."""
    images, blocks = [], []
    for raw_path in paths:
        path = Path(raw_path)
        suffix = path.suffix.lower()
        size = path.stat().st_size
        if suffix in IMAGE_EXTENSIONS:
            if size > 15 * 1024 * 1024:
                raise ValueError(f"图片过大（上限 15 MB）：{path.name}")
            images.append(str(path))
        elif suffix in TEXT_EXTENSIONS:
            if size > 200_000:
                raise ValueError(f"代码/文本附件过大（上限 200 KB）：{path.name}")
            content = path.read_text(encoding="utf-8", errors="replace")
            try:
                display_path = path.resolve().relative_to(Path(workspace).resolve()).as_posix() if workspace else path.name
            except ValueError:
                display_path = str(path.resolve())
            blocks.append(f"\n\n--- 附件 {display_path} ---\n{content}\n--- 附件结束 ---")
        else:
            raise ValueError(f"暂不支持此文件类型：{path.name}")
    if blocks:
        prompt = "以下代码/文本附件内容已直接包含在本条消息中，请直接阅读分析，不要再用文件工具按附件名重新读取。\n" + prompt
    return prompt + "".join(blocks), images


def ensure_ollama() -> tuple[bool, str]:
    """检测 Ollama，没运行就自动启动。"""
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
        return True, "已运行"
    except Exception:
        pass
    if not OLLAMA_EXE.exists():
        return False, f"找不到 Ollama：{OLLAMA_EXE}"
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = str(OLLAMA_MODELS)
    env.setdefault("OLLAMA_LLM_LIBRARY", "vulkan")
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen([str(OLLAMA_EXE), "serve"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except Exception as e:
        return False, f"启动失败：{e}"
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
            return True, "已自动启动"
        except Exception:
            continue
    return False, "启动超时"


def load_config() -> dict:
    defaults = {"base_url": "http://127.0.0.1:11434",
                "model": "qwen2.5-coder:7b",
                "workspace": "D:/Program/AI_Model_agent",
                "max_steps": 30, "max_context_messages": 16}
    if CONFIG_PATH.exists():
        try:
            defaults.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return defaults


class AgentApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("本地 AI Agent")
        # Keep the initial window below typical Windows high-DPI work areas.
        self.root.geometry("1000x650")
        self.root.minsize(760, 540)
        self.root.configure(bg="#f8f9fb")
        try:
            self.root.iconbitmap(str(BASE / "agent.ico"))
        except tk.TclError:
            pass

        self.cfg = load_config()
        from core.sessions import SessionStore
        self.session_store = SessionStore(BASE / "data" / "history.sqlite3")
        self.current_session_id = None
        self.session_turns: list[dict] = []
        self.history_session_ids: list[str] = []
        self.sidebar_pages = {}
        self.workspace = tk.StringVar(value=self.cfg.get("workspace", "."))
        self.model_var = tk.StringVar(value=self.cfg.get("model", ""))
        self.command_shell = tk.StringVar(value=self.cfg.get("command_shell", "PowerShell"))
        self.full_local_access = tk.BooleanVar(value=bool(self.cfg.get("full_local_access", False)))
        self.auto_run_commands = tk.BooleanVar(value=bool(self.cfg.get("auto_run_commands", True)))
        self.is_admin = self._is_admin_process()
        self.running = False
        self.msg_queue: queue.Queue = queue.Queue()
        self.agent = None
        self.agent_workspace = None
        self.agent_model = None
        self.provider = None
        self.tools_enabled = True
        self.current_capabilities: list[str] = []
        self.capabilities_by_model: dict[str, list[str]] = {}
        self.msg_count = 0
        self.attachments: list[str] = []
        self.streamed_any = False
        self.stream_segment_started = False
        self.stream_segment_start = None
        self.stream_buffer = ""
        self.stream_flush_pending = False
        self.generated_image_refs = []
        self.thinking_chars = 0
        self.task_started_at: float | None = None
        self.task_last_status_at = 0.0
        self.task_category = "chat"
        self.task_phase = ""
        self.task_output_chars = 0
        self.task_external_progress: dict | None = None
        self.task_progress_floor = 0
        self.progress_samples = self.cfg.get("progress_samples", {})
        self.task_estimate_seconds = 30.0
        self.task_run_id = 0
        self.task_failed = False
        self.knowledge_window = None
        self.knowledge_list = None
        self.skill_list = None
        self.mcp_list = None
        self.mcp_name_var = None
        self.mcp_command_var = None
        self.mcp_args_var = None
        self.mcp_env_var = None
        self.split_ratio = 0.72

        self._build_styles()
        self._build_ui()
        self.root.after(0, self._refresh_git)
        threading.Thread(target=self._init_backend, daemon=True).start()
        self.root.after(100, self._poll_queue)
        self.input.focus_set()

    # ---------- 样式 ----------
    def _build_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=C_BG, background=C_BG_SOFT,
                        bordercolor=C_BORDER, arrowcolor=C_TEXT_DIM, borderwidth=0)
        style.configure("TFrame", background=C_BG)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT)
        style.configure("Sidebar.TNotebook.Tab", padding=(3, 4), font=(FONT_UI, 8), width=4)

    # ---------- 界面 ----------
    def _build_ui(self):
        # pack 顺序关键：先上下边缘元素，最后放可扩展的主体，
        # 否则主体会吃掉全部空间把底部输入框挤出窗口（上一版的 bug）。

        # ---- 顶栏 ----
        top = tk.Frame(self.root, bg=C_BG, highlightbackground=C_BORDER, highlightthickness=1)
        top.pack(fill=tk.X, side=tk.TOP, padx=0, pady=(0, 8), ipady=8)

        self.sidebar_btn = tk.Button(top, text="☰", font=(FONT_UI, 12), bg=C_BG,
                                     fg=C_TEXT_DIM, bd=0, relief="flat", cursor="hand2",
                                     command=self._toggle_log, padx=10)
        add_hover_effect(self.sidebar_btn, C_BG, "#f1f3f6")
        self.sidebar_btn.pack(side=tk.LEFT, padx=(6, 3))
        tk.Label(top, text="Agent", font=(FONT_UI, 13, "bold"),
                 bg=C_BG, fg=C_TEXT).pack(side=tk.LEFT)
        tk.Label(top, text="  本地模型 · 可选 MCP 扩展", font=(FONT_UI, 9),
                 bg=C_BG, fg=C_TEXT_DIM).pack(side=tk.LEFT, padx=(4, 0))

        # ---- 状态条 ----
        self.status_var = tk.StringVar(value="正在检查后端...")
        status_frame = tk.Frame(self.root, bg=C_BG)
        status_frame.pack(fill=tk.X, side=tk.TOP, padx=18, pady=(0, 2))
        tk.Label(status_frame, textvariable=self.status_var, font=(FONT_UI, 9),
                 bg=C_BG, fg=C_TEXT_DIM, anchor="w").pack(fill=tk.X)
        self.progress_value = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(self.root, maximum=100,
                                            variable=self.progress_value, mode="determinate")
        self.progress_bar.pack(fill=tk.X, side=tk.TOP, padx=18, pady=(0, 6))
        self.progress_bar.pack_forget()

        # Chat and execution log share a draggable horizontal split. The input
        # card lives inside the chat pane so it follows that pane's width.
        self.content = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bd=0,
                                      relief="flat", sashwidth=6,
                                      sashrelief="flat", bg=C_BORDER,
                                      opaqueresize=True)
        self.content.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.content.bind("<Configure>", lambda _event: self.root.after_idle(self._apply_split_ratio))
        self.content.bind("<ButtonRelease-1>", self._remember_split_ratio, add="+")
        self.chat_column = tk.Frame(self.content, bg=C_BG)
        self.content.add(self.chat_column, stretch="always", minsize=360)

        # ---- 底部输入区 ----
        bottom = tk.Frame(self.chat_column, bg=C_BG)
        bottom.pack(fill=tk.X, side=tk.BOTTOM, pady=(6, 14))

        card = tk.Frame(bottom, bg=C_BG, highlightbackground=C_BORDER, highlightthickness=1)
        self.input_card = card
        card.pack(fill=tk.X, padx=20)
        bottom.bind("<Configure>", self._resize_input_card)

        self.input = tk.Text(card, height=2, wrap=tk.WORD, font=(FONT_UI, 10),
                             bg=C_BG, fg=C_TEXT, bd=0, relief="flat",
                             insertbackground=C_BLUE, padx=14, pady=10,
                             width=72, spacing1=1, spacing3=1)
        self.input.pack(fill=tk.X)
        bind_chat_shortcuts(self.input, self._send)
        self.input.bind("<FocusIn>", lambda e: self._placeholder(False))
        self.input.bind("<FocusOut>", lambda e: self._placeholder(True))
        self._placeholder(True)

        row = tk.Frame(card, bg=C_BG)
        row.pack(fill=tk.X, padx=10, pady=(0, 8))
        self.attach_btn = tk.Button(row, text="＋ 文件 / 图片", font=(FONT_UI, 9), bg=C_BG_SOFT,
                                    fg=C_TEXT_DIM, bd=0, relief="flat", cursor="hand2",
                                    command=self._choose_files)
        add_hover_effect(self.attach_btn, C_BG_SOFT, "#e8edf5")
        self.attach_btn.pack(side=tk.LEFT, padx=(0, 7))
        self.attachment_label = tk.Label(row, text="", font=(FONT_UI, 8), bg=C_BG, fg=C_TEXT_DIM)
        self.attachment_label.pack(side=tk.LEFT, padx=4)
        self.send_btn = tk.Button(row, text="↑", font=("Segoe UI", 13, "bold"),
                                  bg=C_BLUE, fg="white", bd=0, relief="flat",
                                  activebackground=C_BLUE_DARK, cursor="hand2",
                                  width=3, command=self._send)
        add_hover_effect(self.send_btn, C_BLUE, C_BLUE_DARK)
        self.send_btn.pack(side=tk.RIGHT)

        # ---- 功能侧边栏（由 ☰ 图标开合）----
        self.log_panel = tk.Frame(self.content, bg=C_BG_SOFT, width=330)
        self.sidebar = ttk.Notebook(self.log_panel, style="Sidebar.TNotebook")
        self.sidebar.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._build_sidebar_tabs()
        self.log_visible = False

        # ---- 主体：欢迎页 / 对话区 ----
        self.body = tk.Frame(self.chat_column, bg=C_BG)
        self.body.pack(fill=tk.BOTH, side=tk.TOP, expand=True, padx=18)

        self.welcome = tk.Frame(self.body, bg=C_BG)
        logo = tk.Label(self.welcome, text="✳", font=(FONT_UI, 28, "bold"),
                        bg=C_BG, fg=C_BLUE)
        logo.pack(pady=(36, 8))
        self.welcome_title = tk.Label(self.welcome, text="", font=(FONT_UI, 21, "bold"),
                                      bg=C_BG, fg=C_TEXT)
        self.welcome_title.pack()
        tk.Label(self.welcome, text="选好模型和工作空间，直接输入任务\n可附图分析，也可在知识库里导入工作文档",
                 font=(FONT_UI, 10), bg=C_BG, fg=C_TEXT_DIM, justify="center").pack(pady=(10, 0))
        suggestions = tk.Frame(self.welcome, bg=C_BG)
        suggestions.pack(pady=24)
        for label, prompt in (("检查项目结构", "检查当前项目的目录结构，并告诉我主要模块分别负责什么。"),
                              ("修复代码问题", "先检查项目里最明显的问题，说明原因并直接修复。"),
                              ("解释附件图片", "请分析我附上的图片，描述内容并回答我的问题。")):
            tk.Button(suggestions, text=label, font=(FONT_UI, 9), bg=C_BG_SOFT,
                      fg=C_TEXT, bd=0, padx=14, pady=10, cursor="hand2",
                      activebackground="#e9edf5",
                      command=lambda p=prompt: self._use_suggestion(p)).pack(side=tk.LEFT, padx=5)

        self.chat = tk.Text(self.body, wrap=tk.WORD, font=(FONT_UI, 10),
                            bg=C_BG, fg=C_TEXT, bd=0, relief="flat",
                            padx=4, pady=8, spacing1=6, spacing3=6, state="disabled")
        self.chat.tag_configure("user", font=(FONT_UI, 10), foreground=C_TEXT,
                                background=C_USER_BUBBLE, justify="right",
                                lmargin1=80, lmargin2=80, rmargin=16, spacing1=6, spacing3=6)
        self.chat.tag_configure("agent", font=(FONT_UI, 10), foreground=C_TEXT,
                                lmargin1=16, lmargin2=16, rmargin=80, spacing1=6, spacing3=6)
        self.chat.tag_configure("agent_name", font=(FONT_UI, 9, "bold"), foreground=C_BLUE)
        self.chat.tag_configure("user_name", font=(FONT_UI, 9, "bold"), foreground=C_TEXT_DIM)
        self.chat.tag_configure("system", font=(FONT_UI, 9), foreground=C_TEXT_DIM,
                                justify="center", spacing1=10, spacing3=10)
        self.chat.tag_configure("md_code", font=("Consolas", 9), foreground="#3f4752",
                                background="#f1f3f5", lmargin1=12, lmargin2=12,
                                rmargin=12, spacing1=3, spacing3=3)
        self.chat.tag_configure("md_inline_code", font=("Consolas", 9), foreground="#a23b4a",
                                background="#f1f3f5")
        self.chat.tag_configure("md_bold", font=(FONT_UI, 10, "bold"))
        self.chat.tag_configure("md_italic", font=(FONT_UI, 10, "italic"))
        self.chat.tag_configure("md_heading", font=(FONT_UI, 12, "bold"), spacing1=8, spacing3=4)

        self._update_welcome()

    def _build_sidebar_tabs(self):
        def page(title):
            frame = tk.Frame(self.sidebar, bg=C_BG)
            self.sidebar.add(frame, text=title)
            self.sidebar_pages[title] = frame
            return frame

        # Conversation history
        history = page("会话")
        tk.Label(history, text="本机保存的对话", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 9), anchor="w").pack(fill=tk.X, padx=10, pady=(10, 4))
        self.history_list = tk.Listbox(history, font=(FONT_UI, 9), activestyle="none",
                                       bd=0, highlightthickness=1, highlightbackground=C_BORDER)
        self.history_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.history_list.bind("<Double-Button-1>", lambda _e: self._load_selected_session())
        buttons = tk.Frame(history, bg=C_BG)
        buttons.pack(fill=tk.X, padx=8, pady=8)
        tk.Button(buttons, text="新对话", command=self._clear_chat, bg=C_BLUE,
                  fg="white", bd=0, padx=10, pady=6).pack(side=tk.LEFT)
        tk.Button(buttons, text="打开", command=self._load_selected_session,
                  bg=C_BG_SOFT, bd=0, padx=10, pady=6).pack(side=tk.LEFT, padx=6)
        tk.Button(buttons, text="删除历史", command=self._delete_selected_session,
                  bg=C_BG_SOFT, bd=0, padx=10, pady=6).pack(side=tk.RIGHT)

        # Local knowledge base
        knowledge = page("知识")
        tk.Label(knowledge, text="全局本地索引，可跨工作空间使用", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 9), anchor="w").pack(fill=tk.X, padx=10, pady=(10, 4))
        self.knowledge_list = tk.Listbox(knowledge, font=(FONT_UI, 9), activestyle="none",
                                         bd=0, highlightthickness=1, highlightbackground=C_BORDER)
        self.knowledge_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        controls = tk.Frame(knowledge, bg=C_BG)
        controls.pack(fill=tk.X, padx=8, pady=8)
        tk.Button(controls, text="添加 / 重建索引", command=self._add_knowledge_files,
                  bg=C_BLUE, fg="white", bd=0, padx=8, pady=6).pack(side=tk.LEFT)
        tk.Button(controls, text="移除", command=self._remove_knowledge_file,
                  bg=C_BG_SOFT, bd=0, padx=10, pady=6).pack(side=tk.RIGHT)

        # Terminal
        terminal = page("终端")
        tk.Label(terminal, text="后台命令（按当前 Windows 账户运行；不是 OS 沙盒）", bg=C_BG,
                 fg=C_TEXT_DIM, font=(FONT_UI, 8), anchor="w").pack(fill=tk.X, padx=10, pady=(10, 4))
        self.command_shell_combo = ttk.Combobox(terminal, textvariable=self.command_shell,
                                                values=["PowerShell", "CMD"], state="readonly", width=14)
        self.command_shell_combo.pack(anchor="w", padx=10, pady=(0, 6))
        self.command_shell_combo.bind("<<ComboboxSelected>>",
                                      lambda _e: (self._save_cfg(), self._reset_agent()))
        command_row = tk.Frame(terminal, bg=C_BG)
        command_row.pack(fill=tk.X, padx=8)
        self.terminal_entry = tk.Entry(command_row, font=(FONT_UI, 9), relief="solid", bd=1)
        self.terminal_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self.terminal_entry.bind("<Return>", lambda _e: (self._run_manual_command(), "break")[1])
        self.terminal_run_btn = tk.Button(command_row, text="运行", command=self._run_manual_command,
                                          bg=C_BLUE, fg="white", bd=0, padx=10, pady=5)
        self.terminal_run_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self.terminal_output = tk.Text(terminal, wrap=tk.WORD, font=("Consolas", 8),
                                       bg=C_BG_SOFT, bd=0, padx=8, pady=6)
        self.terminal_output.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Git
        git_page = page("Git")
        tk.Label(git_page, text="当前工作空间 · 本地与远端 Git", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 9), anchor="w").pack(fill=tk.X, padx=10, pady=(8, 3))
        tk.Label(git_page, text="提交身份（姓名 / 邮箱）", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 7), anchor="w").pack(fill=tk.X, padx=10)
        git_identity = tk.Frame(git_page, bg=C_BG)
        git_identity.pack(fill=tk.X, padx=8, pady=2)
        self.git_user_var = tk.StringVar()
        self.git_email_var = tk.StringVar()
        tk.Entry(git_identity, textvariable=self.git_user_var, font=(FONT_UI, 8),
                 relief="solid", bd=1).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3), ipady=3)
        tk.Entry(git_identity, textvariable=self.git_email_var, font=(FONT_UI, 8),
                 relief="solid", bd=1).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        git_identity_btn = tk.Frame(git_page, bg=C_BG)
        git_identity_btn.pack(fill=tk.X, padx=8, pady=2)
        tk.Button(git_identity_btn, text="保存本地身份", command=self._git_configure_identity,
                  bg=C_BG_SOFT, bd=0, padx=7, pady=4).pack(side=tk.LEFT)
        tk.Button(git_identity_btn, text="初始化本地仓库", command=self._git_initialize,
                  bg=C_BG_SOFT, bd=0, padx=7, pady=4).pack(side=tk.RIGHT)
        tk.Label(git_page, text="远端仓库 URL（HTTPS 或 SSH）", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 7), anchor="w").pack(fill=tk.X, padx=10, pady=(3, 0))
        self.git_remote_var = tk.StringVar()
        tk.Entry(git_page, textvariable=self.git_remote_var, font=(FONT_UI, 8),
                 relief="solid", bd=1).pack(fill=tk.X, padx=8, pady=(4, 2), ipady=4)
        git_remote_btn = tk.Frame(git_page, bg=C_BG)
        git_remote_btn.pack(fill=tk.X, padx=8, pady=2)
        tk.Button(git_remote_btn, text="设置 origin", command=self._git_set_remote,
                  bg=C_BLUE, fg="white", bd=0, padx=8, pady=4).pack(side=tk.LEFT)
        tk.Button(git_remote_btn, text="克隆仓库…", command=self._git_clone,
                  bg=C_BG_SOFT, bd=0, padx=8, pady=4).pack(side=tk.RIGHT)
        git_sync_btn = tk.Frame(git_page, bg=C_BG)
        git_sync_btn.pack(fill=tk.X, padx=8, pady=2)
        tk.Button(git_sync_btn, text="拉取（快进）", command=self._git_pull,
                  bg=C_BG_SOFT, bd=0, padx=7, pady=4).pack(side=tk.LEFT)
        tk.Button(git_sync_btn, text="推送", command=self._git_push,
                  bg=C_BG_SOFT, bd=0, padx=7, pady=4).pack(side=tk.LEFT, padx=4)
        tk.Button(git_sync_btn, text="刷新状态", command=self._refresh_git,
                  bg=C_BG_SOFT, bd=0, padx=7, pady=4).pack(side=tk.RIGHT)
        self.git_commit_var = tk.StringVar()
        tk.Label(git_page, text="提交说明", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 7), anchor="w").pack(fill=tk.X, padx=10, pady=(3, 0))
        git_commit_row = tk.Frame(git_page, bg=C_BG)
        git_commit_row.pack(fill=tk.X, padx=8, pady=(2, 3))
        tk.Entry(git_commit_row, textvariable=self.git_commit_var, font=(FONT_UI, 8),
                 relief="solid", bd=1).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        tk.Button(git_commit_row, text="暂存并提交", command=self._git_commit,
                  bg=C_BG_SOFT, bd=0, padx=7, pady=4).pack(side=tk.RIGHT, padx=(4, 0))
        self.git_output = tk.Text(git_page, wrap=tk.WORD, font=("Consolas", 8),
                                  bg=C_BG_SOFT, bd=0, padx=8, pady=6)
        self.git_output.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # Skills
        skills_page = page("技能")
        tk.Label(skills_page, text="添加包含 SKILL.md 的技能目录", bg=C_BG,
                 fg=C_TEXT_DIM, font=(FONT_UI, 9), anchor="w").pack(fill=tk.X, padx=10, pady=(10, 4))
        self.skill_list = tk.Listbox(skills_page, font=(FONT_UI, 9), activestyle="none",
                                     bd=0, highlightthickness=1, highlightbackground=C_BORDER)
        self.skill_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        skill_controls = tk.Frame(skills_page, bg=C_BG)
        skill_controls.pack(fill=tk.X, padx=8, pady=8)
        tk.Button(skill_controls, text="导入 Skill 文件夹", command=self._import_skill,
                  bg=C_BLUE, fg="white", bd=0, padx=8, pady=6).pack(side=tk.LEFT)
        tk.Button(skill_controls, text="重载", command=self._refresh_skills,
                  bg=C_BG_SOFT, bd=0, padx=10, pady=6).pack(side=tk.RIGHT)

        # MCP-backed integrations
        mcp_page = page("MCP")
        tk.Label(mcp_page, text="浏览器 / 邮箱 / GitHub 通过 MCP 接入", bg=C_BG,
                 fg=C_TEXT, font=(FONT_UI, 9, "bold"), anchor="w").pack(fill=tk.X, padx=10, pady=(10, 3))
        tk.Label(mcp_page, text="需安装对应 MCP 服务并自行完成账号授权。每次调用会询问确认。",
                 bg=C_BG, fg=C_TEXT_DIM, font=(FONT_UI, 8), justify="left", wraplength=290,
                 anchor="w").pack(fill=tk.X, padx=10, pady=(0, 6))
        tk.Label(mcp_page, text="敏感凭据请使用环境变量引用，例如 ${GITHUB_TOKEN}，不要把令牌直接写入配置。",
                 bg=C_BG, fg=C_TEXT_DIM, font=(FONT_UI, 7), justify="left", wraplength=290,
                 anchor="w").pack(fill=tk.X, padx=10, pady=(0, 4))
        self.mcp_list = tk.Listbox(mcp_page, height=4, font=(FONT_UI, 8), activestyle="none",
                                   bd=0, highlightthickness=1, highlightbackground=C_BORDER)
        self.mcp_list.pack(fill=tk.X, padx=8, pady=4)
        self.mcp_list.bind("<<ListboxSelect>>", self._load_selected_mcp)
        self.mcp_name_var = tk.StringVar()
        self.mcp_command_var = tk.StringVar()
        self.mcp_args_var = tk.StringVar(value="[]")
        self.mcp_env_var = tk.StringVar(value="{}")
        for label, variable, example in (("名称", self.mcp_name_var, "浏览器/邮箱/GitHub 的标识"),
                                         ("启动命令", self.mcp_command_var, "例如 npx 或 python"),
                                         ("参数（JSON 数组）", self.mcp_args_var, '["-y", "@package/server"]'),
                                         ("环境变量（JSON 对象）", self.mcp_env_var, '{"TOKEN":"${MY_TOKEN}"}')):
            tk.Label(mcp_page, text=label, bg=C_BG, fg=C_TEXT_DIM,
                     font=(FONT_UI, 8), anchor="w").pack(fill=tk.X, padx=10, pady=(3, 0))
            tk.Label(mcp_page, text=example, bg=C_BG, fg="#a3a7ae",
                     font=(FONT_UI, 7), anchor="w").pack(fill=tk.X, padx=10)
            entry = tk.Entry(mcp_page, textvariable=variable, font=(FONT_UI, 8), relief="solid", bd=1)
            entry.pack(fill=tk.X, padx=8, pady=(1, 2), ipady=3)
        mcp_controls = tk.Frame(mcp_page, bg=C_BG)
        mcp_controls.pack(fill=tk.X, padx=8, pady=5)
        tk.Button(mcp_controls, text="添加 / 更新", command=self._save_mcp_server,
                  bg=C_BLUE, fg="white", bd=0, padx=8, pady=5).pack(side=tk.LEFT)
        tk.Button(mcp_controls, text="移除所选", command=self._remove_mcp_server,
                  bg=C_BG_SOFT, bd=0, padx=8, pady=5).pack(side=tk.RIGHT)

        settings = page("设置")
        tk.Label(settings, text="模型", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 9), anchor="w").pack(fill=tk.X, padx=10, pady=(12, 4))
        model_row = tk.Frame(settings, bg=C_BG)
        model_row.pack(fill=tk.X, padx=8)
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var,
                                        state="readonly", font=(FONT_UI, 9))
        self.model_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_changed)
        self.refresh_btn = tk.Button(model_row, text="刷新", command=self._refresh_models,
                                     bg=C_BG_SOFT, fg=C_TEXT_DIM, bd=0, padx=9, pady=5)
        add_hover_effect(self.refresh_btn, C_BG_SOFT, "#e8edf5")
        self.refresh_btn.pack(side=tk.RIGHT, padx=(6, 0))
        tk.Label(settings, text="工作空间", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 9), anchor="w").pack(fill=tk.X, padx=10, pady=(14, 4))
        self.workspace_label = tk.Label(settings, text=self.workspace.get(), bg=C_BG_SOFT,
                                        fg=C_TEXT, font=(FONT_UI, 8), anchor="w",
                                        justify="left", wraplength=280, padx=8, pady=7)
        self.workspace_label.pack(fill=tk.X, padx=8)
        tk.Button(settings, text="选择工作空间", command=self._choose_workspace,
                  bg=C_BG_SOFT, bd=0, padx=10, pady=6).pack(anchor="w", padx=8, pady=6)
        tk.Label(settings, text="CMD / PowerShell 默认值在“终端”页选择。\n切换模型或工作空间后会按新配置重建 Agent。",
                 bg=C_BG, fg=C_TEXT_DIM, font=(FONT_UI, 8), justify="left",
                 anchor="w").pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Label(settings, text=f"Windows 权限：{'管理员' if self.is_admin else '普通用户'}",
                 bg=C_BG, fg=C_TEXT_DIM, font=(FONT_UI, 9), anchor="w").pack(
                     fill=tk.X, padx=10, pady=(14, 3))
        tk.Button(settings, text="以管理员身份重启（弹出 UAC）",
                  command=self._restart_as_admin, bg=C_BG_SOFT, bd=0,
                  padx=10, pady=6).pack(anchor="w", padx=8)
        tk.Button(settings,
                  text=("切换到普通用户模式" if self.is_admin else "当前为普通用户模式"),
                  command=self._restart_as_standard_user,
                  state=(tk.NORMAL if self.is_admin else tk.DISABLED),
                  bg=C_BG_SOFT, bd=0, padx=10, pady=6).pack(anchor="w", padx=8, pady=(4, 0))
        tk.Checkbutton(settings, text="允许访问工作空间以外的本机文件",
                       variable=self.full_local_access,
                       command=self._toggle_full_local_access,
                       bg=C_BG, activebackground=C_BG,
                       font=(FONT_UI, 8), anchor="w").pack(fill=tk.X, padx=8, pady=(9, 0))
        tk.Label(settings, text="开启后 Agent 可读写任意本机路径；删除和工作空间外写入仍需确认。命令权限由下方单独控制。",
                 bg=C_BG, fg="#9a5b19", font=(FONT_UI, 7), wraplength=290,
                 justify="left", anchor="w").pack(fill=tk.X, padx=10, pady=(0, 5))
        tk.Checkbutton(settings, text="允许 Agent 自主运行 PowerShell / CMD 命令",
                       variable=self.auto_run_commands,
                       command=self._toggle_auto_run_commands,
                       bg=C_BG, activebackground=C_BG,
                       font=(FONT_UI, 8), anchor="w").pack(fill=tk.X, padx=8, pady=(5, 0))
        tk.Label(settings, text="开启后 Agent 可根据命令结果继续排错和验证，不再逐条弹确认。命令仍按当前账户权限执行，不是沙盒；管理员模式下会获得管理员权限。",
                 bg=C_BG, fg="#9a5b19", font=(FONT_UI, 7), wraplength=290,
                 justify="left", anchor="w").pack(fill=tk.X, padx=10, pady=(0, 5))

        # Execution log
        execution = page("日志")
        tk.Label(execution, text="Agent 工具调用和模型步骤", bg=C_BG, fg=C_TEXT_DIM,
                 font=(FONT_UI, 9), anchor="w").pack(fill=tk.X, padx=10, pady=(10, 4))
        self.log = tk.Text(execution, wrap=tk.WORD, font=("Consolas", 8),
                           bg=C_BG_SOFT, fg=C_TEXT_DIM, bd=0, relief="flat",
                           padx=8, state="disabled")
        self.log.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 8))
        self._refresh_history_list()
        self._refresh_knowledge_list()
        self._refresh_skills()
        self._refresh_mcp_list()

    def _refresh_history_list(self):
        self.history_list.delete(0, tk.END)
        self.history_session_ids = []
        for session in self.session_store.list_sessions():
            self.history_session_ids.append(session["id"])
            updated = (session.get("updated_at") or "").replace("T", " ")[:16]
            label = f"{session['title']}  ·  {updated}"
            self.history_list.insert(tk.END, label)

    def _load_selected_session(self):
        if self.running:
            return
        selection = self.history_list.curselection()
        if not selection:
            return
        session_id = self.history_session_ids[selection[0]]
        data = self.session_store.load(session_id)
        if not data:
            self._refresh_history_list()
            return
        self.current_session_id = session_id
        session = data["session"]
        self.workspace.set(session["workspace"])
        self.model_var.set(session["model"])
        self.workspace_label.config(text=self.workspace.get())
        self._save_cfg()
        self.session_turns = [{"role": t["role"], "content": t["content"]} for t in data["turns"]]
        self._reset_agent()
        self.msg_count = 0
        self.chat.configure(state="normal")
        self.chat.delete("1.0", tk.END)
        self.chat.configure(state="disabled")
        self._update_welcome()
        for turn in self.session_turns:
            self.msg_count += 1
            self._append_chat(turn["role"], turn["content"])
        self.sidebar_btn.config(fg=C_BLUE)
        self.input.focus_set()

    def _delete_selected_session(self):
        selection = self.history_list.curselection()
        if not selection:
            return
        session_id = self.history_session_ids[selection[0]]
        if not messagebox.askyesno("删除历史对话", "永久删除所选对话及其本机历史记录？"):
            return
        self.session_store.delete(session_id)
        if session_id == self.current_session_id:
            self._clear_chat()
        self._refresh_history_list()

    def _reset_agent(self):
        for client in getattr(self.agent, "mcp_clients", []) if self.agent else []:
            try:
                client.stop()
            except Exception:
                pass
        self.agent = None
        self.agent_workspace = None
        self.agent_model = None

    def _refresh_git(self):
        workspace = self.workspace.get().strip()
        self.git_output.configure(state="normal")
        self.git_output.delete("1.0", tk.END)
        self.git_output.insert(tk.END, "正在读取仓库状态…")
        self.git_output.configure(state="disabled")
        threading.Thread(target=self._read_git_status, args=(workspace,), daemon=True).start()

    def _start_git_action(self, label, operation, clone_target=None):
        if self.running:
            messagebox.showinfo("任务正在运行", "请等待当前 Agent 任务结束后再执行 Git 写操作。")
            return
        self.git_output.configure(state="normal")
        self.git_output.delete("1.0", tk.END)
        self.git_output.insert(tk.END, f"正在{label}…")
        self.git_output.configure(state="disabled")
        status_workspace = str(clone_target or self.workspace.get().strip())
        threading.Thread(target=self._git_action_worker,
                         args=(label, operation, str(clone_target) if clone_target else None,
                               status_workspace),
                         daemon=True).start()

    def _git_action_worker(self, label, operation, clone_target, status_workspace):
        try:
            result = operation()
            if clone_target:
                self.msg_queue.put(("git_cloned", clone_target))
            self.msg_queue.put(("git_result", f"{label}完成。\n\n{result or '（Git 未返回额外输出）'}"))
            self._read_git_status(status_workspace)
        except Exception as exc:
            self.msg_queue.put(("git_result", f"{label}失败：{type(exc).__name__}: {exc}"))

    def _git_initialize(self):
        workspace = self.workspace.get().strip()
        if not messagebox.askyesno(
                "初始化本地 Git 仓库？",
                f"将在当前工作空间创建 main 分支。若没有 .gitignore，会生成默认规则以排除虚拟环境、凭据、模型权重和运行数据。\n\n{workspace}\n\n继续？"):
            return
        from core.git_manager import initialize_repository
        self._start_git_action("初始化本地仓库", lambda: initialize_repository(workspace))

    def _git_configure_identity(self):
        workspace = self.workspace.get().strip()
        name, email = self.git_user_var.get().strip(), self.git_email_var.get().strip()
        if not messagebox.askyesno("保存当前仓库身份？", f"提交署名将设置为：\n{name} <{email}>\n\n仅应用于当前工作空间仓库。继续？"):
            return
        from core.git_manager import configure_identity
        self._start_git_action("保存本地 Git 身份", lambda: configure_identity(workspace, name, email))

    def _git_set_remote(self):
        workspace = self.workspace.get().strip()
        url = self.git_remote_var.get().strip()
        if not url:
            messagebox.showinfo("远端仓库地址", "先粘贴 GitHub/GitLab 仓库的 HTTPS 或 SSH 地址。")
            return
        if not messagebox.askyesno(
                "设置 origin 远端？",
                f"为当前仓库添加或更新 origin：\n{url}\n\n不要把访问令牌写在 URL 中；凭据使用 Git Credential Manager/SSH key。继续？"):
            return
        from core.git_manager import set_remote
        self._start_git_action("设置远端仓库", lambda: set_remote(workspace, url))

    def _git_clone(self):
        url = self.git_remote_var.get().strip()
        if not url:
            messagebox.showinfo("克隆仓库", "先在远端仓库 URL 输入框粘贴仓库地址。")
            return
        parent = filedialog.askdirectory(title="选择克隆到哪个目录")
        if not parent:
            return
        repo_name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if repo_name.lower().endswith(".git"):
            repo_name = repo_name[:-4]
        if not repo_name or repo_name in (".", ".."):
            messagebox.showerror("仓库地址无效", "无法从仓库 URL 识别目录名称。")
            return
        target = Path(parent) / repo_name
        if not messagebox.askyesno("克隆远端仓库？", f"将仓库克隆到：\n{target}\n\n需要 Git 凭据的仓库会由 Git Credential Manager/SSH 处理。继续？"):
            return
        from core.git_manager import clone_repository
        self._start_git_action("克隆仓库", lambda: clone_repository(url, target), clone_target=target)

    def _git_pull(self):
        workspace = self.workspace.get().strip()
        if not messagebox.askyesno("拉取远端更改？", "使用 `git pull --ff-only` 更新当前工作空间。若本地与远端分叉，会停止并报告，不会自动合并。继续？"):
            return
        from core.git_manager import pull_fast_forward
        self._start_git_action("拉取远端", lambda: pull_fast_forward(workspace))

    def _git_push(self):
        workspace = self.workspace.get().strip()
        if not messagebox.askyesno("推送本地提交？", "将当前分支的本地提交推送到 origin。请确认仓库地址和提交内容正确。继续？"):
            return
        from core.git_manager import push_current_branch
        self._start_git_action("推送到 origin", lambda: push_current_branch(workspace))

    def _git_commit(self):
        workspace = self.workspace.get().strip()
        message = self.git_commit_var.get().strip()
        if not message:
            messagebox.showinfo("提交说明", "先填写提交说明。")
            return
        if not messagebox.askyesno("暂存并提交所有未忽略文件？", f"将运行 `git add -A` 并创建本地提交：\n{message}\n\n请先检查 .gitignore，确认没有把密钥或大型模型文件纳入仓库。继续？"):
            return
        from core.git_manager import stage_and_commit
        self._start_git_action("暂存并提交", lambda: stage_and_commit(workspace, message))

    def _read_git_status(self, workspace):
        try:
            identity = []
            for key in ("user.name", "user.email"):
                value = subprocess.run(["git", "-C", workspace, "config", "--get", key],
                                       capture_output=True, text=True, timeout=8,
                                       encoding="utf-8", errors="replace",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                identity.append(value.stdout.strip() if value.returncode == 0 else "")
            self.msg_queue.put(("git_identity", tuple(identity)))
            check = subprocess.run(["git", "-C", workspace, "status", "--short", "--branch"],
                                   capture_output=True, text=True, timeout=8,
                                   encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if check.returncode:
                result = "当前工作空间还不是 Git 仓库，或系统找不到 Git。\n\n" + (check.stderr or check.stdout)
            else:
                remote = subprocess.run(["git", "-C", workspace, "remote", "-v"],
                                        capture_output=True, text=True, timeout=8,
                                        encoding="utf-8", errors="replace",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                origin = subprocess.run(["git", "-C", workspace, "remote", "get-url", "origin"],
                                        capture_output=True, text=True, timeout=8,
                                        encoding="utf-8", errors="replace",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.msg_queue.put(("git_remote", origin.stdout.strip() if origin.returncode == 0 else ""))
                recent = subprocess.run(["git", "-C", workspace, "log", "-5", "--oneline"],
                                       capture_output=True, text=True, timeout=8,
                                       encoding="utf-8", errors="replace",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                result = (f"工作空间：{workspace}\n\n状态：\n{check.stdout.strip()}\n\n"
                          f"远程仓库：\n{remote.stdout.strip() or '（未配置远程仓库）'}\n\n"
                          f"最近提交：\n{recent.stdout.strip() or '（暂无提交）'}")
        except FileNotFoundError:
            result = "找不到 git 命令。安装 Git for Windows 后重启 Agent。"
        except Exception as exc:
            result = f"读取 Git 状态失败：{type(exc).__name__}: {exc}"
        self.msg_queue.put(("git_result", result))

    def _refresh_skills(self):
        from core.skills import load_skills
        self.skill_list.delete(0, tk.END)
        self.loaded_skills = load_skills(SKILLS_DIR)
        for skill in self.loaded_skills:
            self.skill_list.insert(tk.END, f"{skill['name']}  ·  {skill['description']}")
        if not self.loaded_skills:
            self.skill_list.insert(tk.END, "尚无 Skills")

    def _import_skill(self):
        if self.running:
            messagebox.showinfo("任务正在运行", "请等当前 Agent 任务完成后再导入 Skill。")
            return
        source = filedialog.askdirectory(title="选择包含 SKILL.md 的技能文件夹")
        if not source:
            return
        source_path = Path(source)
        skill_file = source_path / "SKILL.md"
        if not skill_file.is_file():
            messagebox.showerror("导入失败", "所选文件夹根目录必须包含 SKILL.md。")
            return
        dest = SKILLS_DIR / source_path.name
        if dest.exists() and not messagebox.askyesno("覆盖 Skill", f"{dest.name} 已存在，覆盖它？"):
            return
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source_path, dest)
            self._refresh_skills()
            self._reset_agent()
            messagebox.showinfo("Skill 已添加", f"已导入 {source_path.name}。下一条任务会加载新技能。")
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def _mcp_config(self):
        if not MCP_PATH.exists():
            return {"mcpServers": {}}
        return json.loads(MCP_PATH.read_text(encoding="utf-8"))

    def _refresh_mcp_list(self):
        self.mcp_list.delete(0, tk.END)
        try:
            self.mcp_names = list(self._mcp_config().get("mcpServers", {}))
            for name in self.mcp_names:
                config = self._mcp_config()["mcpServers"][name]
                self.mcp_list.insert(tk.END, f"{name}  ·  {config.get('command', '')}")
            if not self.mcp_names:
                self.mcp_list.insert(tk.END, "未配置 MCP 服务")
        except Exception as exc:
            self.mcp_names = []
            self.mcp_list.insert(tk.END, f"配置读取失败：{exc}")

    def _load_selected_mcp(self, _event=None):
        selection = self.mcp_list.curselection()
        if not selection or selection[0] >= len(getattr(self, "mcp_names", [])):
            return
        name = self.mcp_names[selection[0]]
        config = self._mcp_config()["mcpServers"][name]
        self.mcp_name_var.set(name)
        self.mcp_command_var.set(config.get("command", ""))
        self.mcp_args_var.set(json.dumps(config.get("args", []), ensure_ascii=False))
        self.mcp_env_var.set(json.dumps(config.get("env", {}), ensure_ascii=False))

    def _save_mcp_server(self):
        if self.running:
            messagebox.showinfo("任务正在运行", "请等当前 Agent 任务完成后再修改 MCP 连接器。")
            return
        name, command = self.mcp_name_var.get().strip(), self.mcp_command_var.get().strip()
        try:
            args = json.loads(self.mcp_args_var.get() or "[]")
            env = json.loads(self.mcp_env_var.get() or "{}")
            if not name or not command or not isinstance(args, list) or not isinstance(env, dict):
                raise ValueError("需要名称和启动命令；参数须为 JSON 数组，环境变量须为 JSON 对象。")
        except Exception as exc:
            messagebox.showerror("MCP 配置无效", str(exc))
            return
        if not messagebox.askyesno("允许启动 MCP 服务？",
                                   f"将保存并在下次创建 Agent 时运行命令：\n{command} {' '.join(map(str,args))}\n\nMCP 服务可能访问邮箱、浏览器或 GitHub 数据。只添加可信服务。"):
            return
        try:
            data = self._mcp_config()
            data.setdefault("mcpServers", {})[name] = {"command": command, "args": args, "env": env}
            MCP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._refresh_mcp_list()
            self._reset_agent()
            messagebox.showinfo("MCP 已保存", "已保存连接器配置。每次调用 MCP 工具都会再次询问确认。")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _remove_mcp_server(self):
        if self.running:
            messagebox.showinfo("任务正在运行", "请等当前 Agent 任务完成后再修改 MCP 连接器。")
            return
        selection = self.mcp_list.curselection()
        if not selection or selection[0] >= len(self.mcp_names):
            return
        name = self.mcp_names[selection[0]]
        if not messagebox.askyesno("移除 MCP 服务", f"从 Agent 配置移除 {name}？"):
            return
        data = self._mcp_config()
        data.get("mcpServers", {}).pop(name, None)
        MCP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._refresh_mcp_list()
        self._reset_agent()

    def _run_manual_command(self):
        command = self.terminal_entry.get().strip()
        if not command:
            return
        shell = self.command_shell.get()
        workspace = self.workspace.get().strip()
        if not messagebox.askyesno("执行命令？",
                                   f"在工作空间目录通过 {shell} 后台执行：\n{command}\n\n命令不是 OS 沙盒，会按当前 Windows 用户权限运行。是否继续？"):
            return
        self.terminal_entry.delete(0, tk.END)
        self.command_shell_combo.configure(state="disabled")
        self.terminal_run_btn.configure(state="disabled")
        self._append_terminal(f"> {shell}: {command}\n正在执行…\n\n")
        threading.Thread(target=self._execute_manual_command,
                         args=(command, shell, workspace), daemon=True).start()

    def _execute_manual_command(self, command, shell, workspace):
        from core.tools import execute_shell_command
        try:
            code, output, used_shell = execute_shell_command(command, workspace, shell)
            result = f"[{used_shell} 退出码 {code}]\n{output or '（无输出）'}\n\n"
        except subprocess.TimeoutExpired:
            result = "命令超时（120 秒）\n\n"
        except Exception as exc:
            result = f"命令执行失败：{type(exc).__name__}: {exc}\n\n"
        self.msg_queue.put(("terminal_result", result))

    def _append_terminal(self, text):
        self.terminal_output.configure(state="normal")
        self.terminal_output.insert(tk.END, text)
        self.terminal_output.see(tk.END)
        self.terminal_output.configure(state="disabled")

    # ---------- 欢迎页切换 ----------
    def _update_welcome(self):
        if self.msg_count == 0:
            ws_name = Path(self.workspace.get()).name or self.workspace.get()
            self.welcome_title.config(text=f"我们应该在 {ws_name} 中做些什么？")
            self.welcome.pack(fill=tk.BOTH, expand=True)
            self.chat.pack_forget()
        else:
            self.welcome.pack_forget()
            self.chat.pack(fill=tk.BOTH, expand=True)

    # ---------- 占位文字 ----------
    def _placeholder(self, show: bool):
        if show and not self.input.get("1.0", tk.END).strip():
            self.input.insert("1.0", "随心输入...")
            self.input.tag_add("ph", "1.0", "end")
            self.input.tag_configure("ph", foreground=C_TEXT_DIM)
        elif not show:
            self.input.tag_remove("ph", "1.0", "end")
            if self.input.get("1.0", tk.END).strip() == "随心输入...":
                self.input.delete("1.0", tk.END)

    # ---------- 后端 ----------
    def _init_backend(self):
        ok, msg = ensure_ollama()
        if not ok:
            self.msg_queue.put(("backend_error", msg))
            return
        from core.llm import OllamaProvider
        self.provider = OllamaProvider(base_url=self.cfg["base_url"],
                                       default_model=self.cfg.get("model", ""))
        try:
            models = self.provider.list_models()
        except Exception as e:
            self.msg_queue.put(("backend_error", str(e)))
            return
        self.msg_queue.put(("backend_ready", (models, msg)))

    def _refresh_models(self):
        if not self.provider:
            threading.Thread(target=self._init_backend, daemon=True).start()
            return
        threading.Thread(target=self._load_models, daemon=True).start()

    def _load_models(self):
        try:
            self.msg_queue.put(("models", self.provider.list_models()))
        except Exception as e:
            self.msg_queue.put(("backend_error", str(e)))

    def _on_model_changed(self, _event=None):
        self._save_cfg()
        selected = self.model_var.get()
        if self.provider and hasattr(self.provider, "model_capabilities"):
            threading.Thread(target=self._load_capabilities, args=(selected,), daemon=True).start()
        if self.agent is not None and self.agent_model != self.model_var.get():
            self._reset_agent()
            self._append_chat("system", "模型已切换；下一条消息将使用新模型和独立上下文。")

    def _load_capabilities(self, model):
        try:
            self.msg_queue.put(("capabilities", (model, self.provider.model_capabilities(model))))
        except Exception as e:
            self.msg_queue.put(("backend_error", str(e)))

    def _build_agent(self, workspace=None, model=None):
        from core.tools import ToolRegistry, register_builtin_tools
        from core.agent import Agent, AgentConfig
        from core.skills import load_skills, build_skills_prompt, register_skill_tools
        from core.mcp import load_mcp_servers, register_mcp_tools

        ws = (workspace or self.workspace.get()).strip()
        if not Path(ws).is_dir():
            raise ValueError(f"工作空间不存在：{ws}")

        def confirm(msg):
            result = {"v": False}
            ev = threading.Event()

            def ask():
                result["v"] = messagebox.askyesno("Agent 操作确认", msg)
                ev.set()
            self.root.after(0, ask)
            ev.wait(60)
            return result["v"]

        registry = ToolRegistry(workspace=ws, confirm_callback=confirm,
                                 allow_outside_workspace=self.full_local_access.get())
        register_builtin_tools(registry, allow_dangerous=self.auto_run_commands.get(),
                               default_shell=self.command_shell.get(),
                               image_callback=lambda path: self.msg_queue.put(("generated_image", path)),
                               progress_callback=lambda data: self.msg_queue.put(("task_progress", data)),
                               default_model=model or self.model_var.get())
        skills = load_skills(SKILLS_DIR)
        register_skill_tools(registry, skills)
        skills_prompt = build_skills_prompt(skills)
        clients = load_mcp_servers(MCP_PATH)
        if clients:
            register_mcp_tools(registry, clients)

        capabilities = self.provider.model_capabilities(model) if hasattr(self.provider, "model_capabilities") else ["tools"]
        self.current_capabilities = capabilities
        self.tools_enabled = "tools" in capabilities
        system = "".join((
            "你是一个运行在用户本地电脑上的 AI 助手。默认用中文简洁回答，先给结论，通常 1–3 句；不要重复句子、复述问题或添加客套填充。用户要求步骤或任务较复杂时再分点说明。\n",
            f"当前工作空间的绝对路径是：{Path(ws).resolve()}。用户询问当前目录或文件绝对路径时，直接使用此路径或调用 workspace_info，不要反问用户。\n",
            ("你可以读写、搜索工作空间文件；" + ("run_command 已获用户授权可自主执行，不要再请求逐条确认；根据真实命令输出排查错误、调整方案并验证结果。" if self.auto_run_commands.get() else "执行命令前会请求用户确认。") + "删除文件、工作空间外写入、生成图片和调用外部 MCP 工具仍会确认。命令按当前 Windows 用户权限运行，但不是操作系统沙盒。\n") if self.tools_enabled else "当前模型不支持工具调用，只能进行对话和视觉分析；不要声称已创建文件、运行命令或生成图片。请建议用户切换到同时支持 tools 的模型（例如已安装的 qwen3.5:4b）。\n",
            "如果用户消息已附上代码或文本文件内容，应直接使用消息中的附件内容；需要调用文件工具时，使用附件标题中给出的完整相对路径。\n",
            "本 GUI 已将 ComfyUI 封装为 generate_image 工具。用户问如何使用时，说明直接在聊天输入‘生成一张……’即可，不需要手工搭节点工作流；明确询问 ComfyUI 节点界面时才介绍工作流。用户要求文生图时调用 generate_image；只有工具返回成功才报告已生成。\n",
            "用户要求启动 VS Code 时调用 open_vscode；要求查询设备时调用 query_devices。只报告工具返回的进程/设备结果，不得编造显示器、硬件、程序版本、系统版本或‘已显示窗口’。工具启动进程不代表窗口已可见。\n",
            "用户要求配置 Git 时，先查看当前工作空间的 git status 和 git remote -v；如果缺少远端 URL，应询问用户。添加远端后再次验证实际地址；没有 push 明确要求前不要推送。\n",
            ("规则：需要操作时直接调用工具，不要只描述。修改文件前先读取现有内容。看完工具结果后继续下一步；失败时读错误、自己调整命令/方案并重试，成功后检查实际状态，直到任务完成。不得把启动进程说成窗口已显示，也不得把退出码或预期当作验证。最后给出简洁总结。\n"
             if self.tools_enabled else "如果用户要求修改本地文件或执行命令，请说明当前所选模型不支持本地工具操作。\n"),
            skills_prompt,
        ))

        def log_cb(m):
            self.msg_queue.put(("log", m))
            if m.strip().startswith("--- 第"):
                self.msg_queue.put(("agent_phase", m.strip()))

        def token_cb(text):
            self.msg_queue.put(("stream_start" if text is None else "stream_token", text or ""))

        def progress_cb(text):
            self.msg_queue.put(("thinking_progress", len(text or "")))

        aconfig = AgentConfig(max_steps=int(self.cfg.get("max_steps", 30)),
                              max_context_messages=int(self.cfg.get("max_context_messages", 16)),
                              verbose=False, tools_enabled=self.tools_enabled,
                              log_callback=log_cb, token_callback=token_cb,
                              progress_callback=progress_cb)
        agent = Agent(self.provider, registry, config=aconfig)
        agent.set_model(model or self.model_var.get())
        agent.messages.append({"role": "system", "content": system})
        for turn in self.session_turns[:-1]:
            if turn.get("role") in ("user", "assistant"):
                agent.messages.append({"role": turn["role"], "content": turn["content"]})
        agent.mcp_clients = clients
        return agent

    # ---------- 交互 ----------
    def _choose_workspace(self):
        path = filedialog.askdirectory(initialdir=self.workspace.get())
        if path:
            self.workspace.set(path)
            self.workspace_label.config(text=path)
            self.git_remote_var.set("")
            self.git_user_var.set("")
            self.git_email_var.set("")
            self._save_cfg()
            self._update_welcome()
            self._refresh_git()
            if self.agent is not None and self.agent_workspace != path:
                self._reset_agent()

    def _choose_files(self):
        paths = filedialog.askopenfilenames(title="添加图片或代码文件", filetypes=[
            ("图片和文本/代码", "*.png *.jpg *.jpeg *.webp *.bmp *.py *.js *.ts *.tsx *.jsx *.html *.css *.json *.md *.txt *.yaml *.yml *.toml *.csv *.log"),
            ("所有文件", "*.*")])
        if paths:
            self.attachments = list(dict.fromkeys(self.attachments + list(paths)))
            names = [Path(p).name for p in self.attachments]
            self.attachment_label.config(text=", ".join(names[:2]) + (f" 等 {len(names)} 张" if len(names) > 2 else ""))

    def _knowledge_index(self):
        from core.rag import LocalKnowledgeBase
        return LocalKnowledgeBase(BASE, BASE / "data", allow_external=True)

    def _manage_knowledge(self):
        if not self.log_visible:
            self._toggle_log()
        self.sidebar.select(self.sidebar_pages["知识"])
        self._refresh_knowledge_list()

    def _refresh_knowledge_list(self):
        if not self.knowledge_list or not self.knowledge_list.winfo_exists():
            return
        self.knowledge_list.delete(0, tk.END)
        try:
            docs = self._knowledge_index().list_documents()
            for doc in docs:
                self.knowledge_list.insert(tk.END, f"{doc['source']}    · {doc['chunks']} 段")
            if not docs:
                self.knowledge_list.insert(tk.END, "知识库为空。点击“添加 / 重新索引”选择本机任意位置的文档。")
        except Exception as exc:
            self.knowledge_list.insert(tk.END, f"读取知识库失败：{exc}")

    def _add_knowledge_files(self):
        from core.rag import SUPPORTED_EXTENSIONS
        patterns = " ".join("*" + ext for ext in sorted(SUPPORTED_EXTENSIONS))
        paths = filedialog.askopenfilenames(title="添加到全局本地知识库",
                                             filetypes=[("支持的文档", patterns), ("所有文件", "*.*")])
        if not paths:
            return
        self.status_var.set(f"正在建立知识库索引：{len(paths)} 个文件…")
        threading.Thread(target=self._index_knowledge_files,
                         args=(list(paths),), daemon=True).start()

    def _index_knowledge_files(self, paths):
        from core.rag import LocalKnowledgeBase
        result = []
        try:
            index = LocalKnowledgeBase(BASE, BASE / "data", allow_external=True)
            for path in paths:
                try:
                    count = index.add_file(path)
                    result.append(f"已索引 {Path(path).name}（{count} 段）")
                except Exception as exc:
                    result.append(f"{Path(path).name}：{exc}")
            self.msg_queue.put(("knowledge_indexed", "\n".join(result)))
        except Exception as exc:
            self.msg_queue.put(("knowledge_indexed", f"建立索引失败：{exc}"))

    def _remove_knowledge_file(self):
        if not self.knowledge_list or not self.knowledge_list.curselection():
            return
        selected = self.knowledge_list.get(self.knowledge_list.curselection()[0])
        source = selected.split("    ·", 1)[0]
        if source.startswith("知识库为空") or source.startswith("读取知识库失败"):
            return
        if not messagebox.askyesno("移除知识库文件", f"从索引中移除 {source}？原文件不会删除。"):
            return
        try:
            self._knowledge_index().remove_file(source)
            self._refresh_knowledge_list()
        except Exception as exc:
            messagebox.showerror("移除失败", str(exc))

    def _use_suggestion(self, prompt):
        self._placeholder(False)
        self.input.delete("1.0", tk.END)
        self.input.insert("1.0", prompt)
        self.input.focus_set()

    def _save_cfg(self):
        self.cfg["model"] = self.model_var.get()
        self.cfg["workspace"] = self.workspace.get()
        self.cfg["command_shell"] = self.command_shell.get()
        self.cfg["full_local_access"] = self.full_local_access.get()
        self.cfg["auto_run_commands"] = self.auto_run_commands.get()
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception:
            pass
        if self.current_session_id:
            try:
                self.session_store.update_context(self.current_session_id,
                                                  self.workspace.get().strip(),
                                                  self.model_var.get())
            except Exception:
                pass

    def _toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.content.add(self.log_panel, stretch="always", minsize=220)
            self.sidebar_btn.config(text="×", fg=C_BLUE)
            self.root.after_idle(self._apply_split_ratio)
        else:
            self.content.forget(self.log_panel)
            self.sidebar_btn.config(text="☰", fg=C_TEXT_DIM)
            self.root.after_idle(self._resize_input_card)

    def _resize_input_card(self, _event=None):
        if not hasattr(self, "input_card"):
            return
        width = self.chat_column.winfo_width()
        if width <= 1:
            return
        self.input_card.pack_configure(padx=responsive_input_margin(width))

    def _apply_split_ratio(self):
        if not self.log_visible:
            return
        width = self.content.winfo_width()
        if width <= 1:
            return
        try:
            self.content.sash_place(0, int(width * self.split_ratio), 0)
            self._resize_input_card()
        except tk.TclError:
            pass

    def _remember_split_ratio(self, _event=None):
        if not self.log_visible:
            return
        width = self.content.winfo_width()
        if width <= 1:
            return
        try:
            x, _y = self.content.sash_coord(0)
            self.split_ratio = max(0.45, min(0.85, x / width))
        except tk.TclError:
            pass

    def _clear_chat(self):
        if self.running:
            return
        self.chat.configure(state="normal")
        self.chat.delete("1.0", tk.END)
        self.chat.configure(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")
        self._reset_agent()
        self.current_session_id = None
        self.session_turns = []
        self.msg_count = 0
        self._update_welcome()

    def _send(self):
        if self.running:
            return
        self._placeholder(False)
        text = self.input.get("1.0", tk.END).strip()
        if not text or text == "随心输入...":
            return
        if not self.provider:
            messagebox.showerror("未就绪", "Ollama 后端未就绪，无法执行任务。")
            return
        attachments = list(self.attachments)
        selected_model = self.model_var.get()
        auto_switch_notice = None
        known_capabilities = {
            "qwen2.5vl:7b": ["vision"],
            "qwen2.5-coder:7b": ["tools"],
            "deepseek-r1:8b": ["tools", "thinking"],
            "qwen3.5:4b": ["tools", "vision", "thinking"],
        }
        capabilities = self.capabilities_by_model.get(selected_model, known_capabilities.get(selected_model, []))
        if "tools" not in capabilities and self._looks_like_action_request(text):
            available = list(self.model_combo.cget("values"))
            fallback = choose_tool_model(available, self.capabilities_by_model,
                                         needs_vision=bool(attachments))
            if fallback:
                self.model_var.set(fallback)
                self._save_cfg()
                self._reset_agent()
                auto_switch_notice = f"当前模型不支持工具，已自动切换到 {fallback} 完成这个操作任务。"
                selected_model = fallback
                self.current_capabilities = self.capabilities_by_model.get(fallback, ["tools", "vision"])
                self.tools_enabled = True
            else:
                self._append_chat("system", "当前模型不支持工具，无法替你运行命令、操作文件或启动应用。请先选择 qwen3.5:4b、deepseek-r1:8b 或 qwen2.5-coder:7b。")
                self.status_var.set("所选模型不支持 Agent 工具；未执行操作")
                return
        self.input.delete("1.0", tk.END)
        self._placeholder(True)
        self.input.focus_set()
        self.msg_count += 1
        self._update_welcome()
        display_text = text + ("\n\n附件：" + ", ".join(Path(p).name for p in attachments) if attachments else "")
        self._append_chat("user", display_text)
        if auto_switch_notice:
            self._append_chat("system", auto_switch_notice)
        if self.current_session_id is None:
            self.current_session_id = self.session_store.create(
                text, self.workspace.get().strip(), self.model_var.get())
        self.session_turns.append({"role": "user", "content": display_text})
        self.session_store.append_turn(self.current_session_id, "user", display_text)
        self._refresh_history_list()
        self.streamed_any = False
        self.stream_segment_started = False
        self.thinking_chars = 0
        from core.progress import classify_task, expected_seconds
        self.task_started_at = time.monotonic()
        self.task_category = classify_task(text)
        self.task_estimate_seconds = expected_seconds(selected_model, self.task_category,
                                                      self.progress_samples)
        self.task_phase = "模型思考"
        self.task_output_chars = 0
        self.task_external_progress = None
        self.task_progress_floor = 0
        self.task_failed = False
        self.task_last_status_at = 0.0
        self.task_run_id += 1
        run_id = self.task_run_id
        self.progress_value.set(0)
        if not self.progress_bar.winfo_manager():
            self.progress_bar.pack(fill=tk.X, side=tk.TOP, padx=18, pady=(0, 6),
                                   before=self.content)
        self.attachments.clear()
        self.attachment_label.config(text="")
        self.running = True
        self.send_btn.configure(state="disabled", bg="#b9c9e8")
        self.status_var.set("Agent 正在思考 · 已用 0秒 · 估算 0%")
        self._refresh_progress_status(force=True)
        threading.Thread(target=self._run_task,
                         args=(text, self.workspace.get().strip(), selected_model, attachments),
                         daemon=True).start()

    def _refresh_progress_status(self, force=False):
        if not self.running or self.task_started_at is None:
            return
        now = time.monotonic()
        if not force and now - self.task_last_status_at < 0.5:
            return
        self.task_last_status_at = now
        from core.progress import estimated_progress, format_duration
        elapsed = now - self.task_started_at
        external = self.task_external_progress
        if external is not None:
            percent = max(0, min(99, int(external.get("percent", 0))))
            remaining = external.get("remaining")
            if remaining is None:
                _, remaining = estimated_progress(elapsed, self.task_estimate_seconds)
            phase = external.get("stage", self.task_phase)
        else:
            percent, remaining = estimated_progress(elapsed, self.task_estimate_seconds)
            phase = self.task_phase
        self.task_progress_floor = max(self.task_progress_floor, percent)
        percent = self.task_progress_floor
        detail = f" · 已输出 {self.task_output_chars:,} 字" if self.task_output_chars else ""
        self.progress_value.set(percent)
        self.status_var.set(
            f"{phase} · 已用 {format_duration(elapsed)} · 预计剩余约 {format_duration(remaining)} · {percent}%（估算）{detail}"
        )

    def _finish_progress(self):
        if self.task_started_at is None:
            return
        from core.progress import format_duration
        elapsed = max(0.0, time.monotonic() - self.task_started_at)
        key = f"{self.agent_model or self.model_var.get()}|{self.task_category}"
        samples = list(self.progress_samples.get(key, []))
        samples.append(round(elapsed, 1))
        self.progress_samples[key] = samples[-5:]
        self.cfg["progress_samples"] = self.progress_samples
        self._save_cfg()
        run_id = self.task_run_id
        self.progress_value.set(100)
        status = "本轮失败" if self.task_failed else "本轮完成"
        self.status_var.set(f"{status} · 用时 {format_duration(elapsed)} · 100%")
        self.task_started_at = None
        self.root.after(1800, lambda: self.progress_bar.pack_forget()
                        if run_id == self.task_run_id and not self.running else None)

    @staticmethod
    def _looks_like_action_request(text):
        return bool(re.search(
            r"打开|启动|运行|执行|创建|新建|删除|修改|编辑|写入|查询|查|检查|排查|修复|解决|处理|定位|编译|部署|配置|安装|卸载|更新|测试|电脑|外部设备|"
            r"设备|外设|操作系统|系统版本|Windows|Win11|Win10|"
            r"生成图片|生成.{0,12}(?:图|照片|插画)|画.{0,12}(?:图|图片|插画)|生图|"
            r"控制|安装|下载|文件|路径|VS\s*Code|VSCode|PowerShell|CMD|命令|Git|仓库|远端|origin|克隆|提交|推送|拉取",
            text, re.I))

    def _run_task(self, text, workspace, model, attachments=None):
        try:
            if (self.agent is None or self.agent_workspace != workspace
                    or self.agent_model != model):
                self.agent = self._build_agent(workspace, model)
                self.agent_workspace = workspace
                self.agent_model = model
            full_prompt, images = prepare_attachments(attachments or [], text, workspace)
            from core.rag import LocalKnowledgeBase
            knowledge = LocalKnowledgeBase(BASE, BASE / "data", allow_external=True).search(text, limit=3)
            if knowledge:
                references = []
                remaining_chars = 1800
                for item in knowledge:
                    if remaining_chars <= 0:
                        break
                    location = f"{item['source']} · 第 {item['page']} 页" if item["page"].isdigit() else item["source"]
                    excerpt = item["content"][:remaining_chars]
                    remaining_chars -= len(excerpt)
                    references.append(f"[来源：{location}]\n{excerpt}")
                full_prompt += (
                    "\n\n以下是从全局本地知识库检索到的参考片段。优先依据片段回答；"
                    "关键结论请引用对应来源名称。若片段没有答案，请明确说明资料中没有依据，不要编造。\n\n"
                    + "\n\n".join(references)
                )
                self.msg_queue.put(("log", f"知识库检索命中 {len(knowledge)} 段：" + ", ".join(dict.fromkeys(i["source"] for i in knowledge))))
            answer = self.agent.run(full_prompt, images=images)
            self.msg_queue.put(("answer", answer))
        except Exception as e:
            self.msg_queue.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            self.msg_queue.put(("done", ""))

    def _poll_queue(self):
        try:
            while True:
                kind, text = self.msg_queue.get_nowait()
                if kind == "log":
                    self._append_log(text)
                    if text.startswith("调用工具: generate_image"):
                        self.task_phase = "准备图片生成"
                    elif text.startswith("调用工具:"):
                        self.task_phase = "正在执行工具"
                    elif text.startswith("结果:"):
                        self.task_phase = "检查工具结果并继续"
                elif kind == "git_result":
                    self.git_output.configure(state="normal")
                    self.git_output.delete("1.0", tk.END)
                    self.git_output.insert(tk.END, text)
                    self.git_output.configure(state="disabled")
                elif kind == "git_identity":
                    self.git_user_var.set(text[0])
                    self.git_email_var.set(text[1])
                elif kind == "git_remote":
                    if text:
                        self.git_remote_var.set(text)
                elif kind == "git_cloned":
                    self.workspace.set(text)
                    self.workspace_label.config(text=text)
                    self._save_cfg()
                    self._reset_agent()
                    self._update_welcome()
                    self._refresh_git()
                elif kind == "terminal_result":
                    self._append_terminal(text)
                    self.command_shell_combo.configure(state="readonly")
                    self.terminal_run_btn.configure(state="normal")
                elif kind == "stream_start":
                    self.task_phase = "模型正在思考"
                    self.task_external_progress = None
                    self._flush_streaming_segment()
                    if self.streamed_any:
                        self.chat.configure(state="normal")
                        self.chat.insert(tk.END, "\n\n")
                        self.chat.configure(state="disabled")
                    self.stream_segment_started = False
                    self.stream_segment_start = None
                    self.stream_buffer = ""
                elif kind == "stream_token":
                    if text:
                        self.task_output_chars += len(text)
                        self.task_phase = ("正在生成代码" if self.task_category == "code"
                                           else "正在生成回答")
                    if not self.stream_segment_started:
                        self.chat.configure(state="normal")
                        self.chat.insert(tk.END, "Agent\n", "agent_name")
                        self.stream_segment_start = self.chat.index("end-1c")
                        self.stream_segment_started = True
                        self.chat.configure(state="disabled")
                    self.stream_buffer += text
                    self.streamed_any = True
                    if not self.stream_flush_pending:
                        self.stream_flush_pending = True
                        self.root.after(80, self._flush_streaming_segment)
                elif kind == "thinking_progress":
                    self.thinking_chars += int(text)
                    self.task_phase = "模型正在思考"
                elif kind == "task_progress":
                    self.task_external_progress = text
                    self.task_phase = text.get("stage", "正在执行工具")
                elif kind == "agent_phase":
                    self.task_external_progress = None
                    self.task_phase = f"Agent 正在分析并执行 {text.strip('- ')}"
                elif kind == "backend_ready":
                    models, launch_msg = text
                    self.provider.default_model = self.cfg.get("model", "")
                    self.model_combo["values"] = models
                    if models and self.model_var.get() not in models:
                        self.model_var.set(models[0])
                    self.status_var.set(f"就绪 · Ollama {launch_msg} · {len(models)} 个本地模型")
                    threading.Thread(target=self._load_capabilities,
                                     args=(self.model_var.get(),), daemon=True).start()
                elif kind == "models":
                    self.model_combo["values"] = text
                    self.status_var.set(f"就绪 · {len(text)} 个本地模型")
                elif kind == "capabilities":
                    model, capabilities = text
                    self.capabilities_by_model[model] = capabilities
                    if model == self.model_var.get():
                        self.current_capabilities = capabilities
                        self.tools_enabled = "tools" in capabilities
                        mode = (" · 仅视觉聊天（qwen3.5:4b 支持看图和工具）"
                                if "vision" in capabilities and not self.tools_enabled
                                else " · Agent 工具可用" if self.tools_enabled else " · 仅对话")
                        self.status_var.set(f"就绪 · 模型 {model}{mode}")
                elif kind == "backend_error":
                    self.status_var.set(f"Ollama 未就绪：{text}")
                    self.model_combo["values"] = [self.model_var.get()]
                elif kind == "answer":
                    self._flush_streaming_segment()
                    self.msg_count += 1
                    if not self.streamed_any:
                        self._append_chat("agent", text or "（本轮完成，但模型没有返回可显示的文本；请查看执行过程。）")
                    elif self.stream_segment_started:
                        self.chat.configure(state="normal")
                        self.chat.insert(tk.END, "\n")
                        self.chat.configure(state="disabled")
                        self.stream_segment_started = False
                    if self.current_session_id:
                        self.session_turns.append({"role": "assistant", "content": text})
                        self.session_store.append_turn(self.current_session_id, "assistant", text)
                        self._refresh_history_list()
                    self.chat.see(tk.END)
                elif kind == "knowledge_indexed":
                    self.status_var.set("知识库索引已更新")
                    self._refresh_knowledge_list()
                    messagebox.showinfo("知识库索引", text)
                elif kind == "error":
                    self.task_failed = True
                    self._append_chat("system", f"[错误] {text}")
                elif kind == "generated_image":
                    self._append_generated_image(text)
                elif kind == "done":
                    self._finish_progress()
                    self.running = False
                    self.send_btn.configure(state="normal", bg=C_BLUE)
                    self._save_cfg()
        except queue.Empty:
            pass
        self._refresh_progress_status()
        self.root.after(100, self._poll_queue)

    # ---------- 显示 ----------
    def _flush_streaming_segment(self):
        """Batch stream updates so rendering cost stays bounded as replies grow."""
        self.stream_flush_pending = False
        if not self.stream_segment_started or self.stream_segment_start is None:
            return
        try:
            self.chat.configure(state="normal")
            self.chat.delete(self.stream_segment_start, tk.END)
            self._insert_markdown(self.chat, self.stream_buffer, "agent")
            self.chat.see(tk.END)
            self.chat.configure(state="disabled")
        except tk.TclError:
            return

    def _append_chat(self, role: str, text: str):
        self.chat.configure(state="normal")
        if role == "user":
            self.chat.insert(tk.END, "你\n", "user_name")
            self._insert_markdown(self.chat, f" {text} \n", "user")
        elif role == "agent":
            self.chat.insert(tk.END, "Agent\n", "agent_name")
            self._insert_markdown(self.chat, text + "\n", "agent")
        else:
            self.chat.insert(tk.END, text + "\n", "system")
        self.chat.see(tk.END)
        self.chat.configure(state="disabled")
        if self.msg_count:
            self._update_welcome()

    def _insert_markdown(self, widget, text, base_tag):
        """Render useful Markdown features directly in the Tk chat transcript."""
        in_code = False
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                widget.insert(tk.END, line, (base_tag, "md_code"))
                continue
            heading = re.match(r"^(#{1,6})\s+(.*)$", line.rstrip("\r\n"))
            if heading:
                widget.insert(tk.END, heading.group(2) + ("\n" if line.endswith("\n") else ""),
                              (base_tag, "md_heading"))
                continue
            parts = re.split(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)", line)
            for part in parts:
                if not part:
                    continue
                if part.startswith("`") and part.endswith("`"):
                    widget.insert(tk.END, part[1:-1], (base_tag, "md_inline_code"))
                elif part.startswith("**") and part.endswith("**"):
                    widget.insert(tk.END, part[2:-2], (base_tag, "md_bold"))
                elif part.startswith("*") and part.endswith("*"):
                    widget.insert(tk.END, part[1:-1], (base_tag, "md_italic"))
                else:
                    widget.insert(tk.END, part, base_tag)

    def _append_generated_image(self, path):
        try:
            image = tk.PhotoImage(file=path)
            factor = max(1, (image.width() + 599) // 600, (image.height() + 399) // 400)
            if factor > 1:
                image = image.subsample(factor, factor)
            self.generated_image_refs.append(image)
            self.chat.configure(state="normal")
            self.chat.insert(tk.END, "\n图片生成完成\n", "agent_name")
            self.chat.image_create(tk.END, image=image, align="top")
            self.chat.insert(tk.END, f"\n{path}\n\n", "agent")
            self.chat.see(tk.END)
            self.chat.configure(state="disabled")
        except Exception as exc:
            self._append_chat("system", f"图片已生成，但无法在对话中预览：{path}（{exc}）")

    def _toggle_full_local_access(self):
        if self.running:
            self.full_local_access.set(not self.full_local_access.get())
            messagebox.showinfo("任务正在运行", "请等当前任务完成后再更改文件访问范围。")
            return
        if self.full_local_access.get() and not messagebox.askyesno(
                "启用完整本地文件访问？",
                "启用后，Agent 的文件工具可以读取和修改工作空间以外的本机文件。"
                "外部写入、删除和命令执行仍会逐次确认。是否启用？"):
            self.full_local_access.set(False)
        self._save_cfg()
        self._reset_agent()

    def _toggle_auto_run_commands(self):
        if self.running:
            self.auto_run_commands.set(not self.auto_run_commands.get())
            messagebox.showinfo("任务正在运行", "请等当前任务完成后再更改命令执行模式。")
            return
        if self.auto_run_commands.get() and not messagebox.askyesno(
                "启用 Agent 自主命令？",
                "Agent 可不经逐条确认运行 PowerShell/CMD 命令，并依据结果继续排错。命令按当前 Windows 用户权限执行；若本 GUI 以管理员身份启动，命令也会有管理员权限。这不是 OS 沙盒。是否启用？"):
            self.auto_run_commands.set(False)
        self._save_cfg()
        self._reset_agent()

    @staticmethod
    def _is_admin_process():
        if os.name != "nt":
            return False
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _restart_as_admin(self):
        if self.is_admin:
            messagebox.showinfo("管理员模式", "当前 Agent 已以管理员身份运行。")
            return
        if not messagebox.askyesno("以管理员身份重启",
                                   "Windows 将显示 UAC 确认窗口。只有在 UAC 中允许后，"
                                   "新 Agent 进程才会获得管理员权限。继续？"):
            return
        try:
            import ctypes
            if getattr(sys, "frozen", False):
                executable, params = sys.executable, subprocess.list2cmdline(sys.argv[1:])
            else:
                executable = str(Path(sys.executable).with_name("pythonw.exe"))
                script = str(BASE / "agent_gui.py")
                params = subprocess.list2cmdline([script])
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", executable, params, str(BASE), 1)
            if result <= 32:
                raise OSError(f"ShellExecuteW 返回 {result}")
            self.root.destroy()
        except Exception as exc:
            messagebox.showerror("无法提权", f"管理员重启失败：{exc}")

    def _restart_as_standard_user(self):
        if not self.is_admin:
            messagebox.showinfo("普通用户模式", "当前 Agent 已以普通用户身份运行。")
            return
        if self.running:
            messagebox.showinfo("任务正在运行", "请等当前任务完成后再切换权限模式。")
            return
        desktop = Path.home() / "Desktop"
        shortcut = desktop / "本地AI Agent.lnk"
        if not shortcut.exists():
            onedrive = os.environ.get("OneDrive")
            if onedrive:
                shortcut = Path(onedrive) / "Desktop" / "本地AI Agent.lnk"
        if not shortcut.exists():
            messagebox.showerror("找不到桌面快捷方式",
                                 "无法自动切回普通用户模式。请关闭当前窗口，再从开始菜单或桌面快捷方式启动 Agent。")
            return
        if not messagebox.askyesno("切换到普通用户模式？",
                                   "将通过 Windows 桌面启动普通用户版 Agent，然后关闭当前管理员窗口。继续？"):
            return
        try:
            import ctypes
            result = ctypes.windll.shell32.ShellExecuteW(None, "open", str(shortcut), None, None, 1)
            if result <= 32:
                raise OSError(f"ShellExecuteW 返回 {result}")
            self.root.destroy()
        except Exception as exc:
            messagebox.showerror("切换失败", f"无法启动普通用户模式：{exc}")

    def _append_log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert(tk.END, text.strip() + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "150.0")
        self.log.see(tk.END)
        self.log.configure(state="disabled")


def main():
    root = tk.Tk()
    AgentApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
