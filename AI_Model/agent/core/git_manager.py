"""Small Git helpers for the GUI's current workspace."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


DEFAULT_GITIGNORE = """# Python and JavaScript environments
**/__pycache__/
**/.pytest_cache/
**/.venv/
**/venv*/
**/node_modules/
**/build/
**/dist/

# Local settings and credentials
**/.env
**/.env.*
!**/.env.example
**/config.local.json
**/mcp_servers.local.json

# Downloaded models, datasets, runtime state, and generated output
**/models/
**/checkpoints/
**/datasets/
**/data/
**/cache/
**/output/
**/.git.codex-undo/
*.gguf
*.safetensors
*.ckpt
*.onnx
*.pt
*.pth
*.log
"""


class GitError(RuntimeError):
    pass


def run_git(workspace: str | Path, args: list[str], timeout: int = 60) -> str:
    """Run git without a shell, returning stdout or a useful error."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise GitError(f"工作空间不存在：{root}")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise GitError("找不到 Git for Windows；请安装后重启 Agent。") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"Git 操作超时：git {' '.join(args)}") from exc
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode:
        raise GitError(error or output or f"git {' '.join(args)} 失败，退出码 {result.returncode}")
    return output


def is_repository(workspace: str | Path) -> bool:
    try:
        return run_git(workspace, ["rev-parse", "--is-inside-work-tree"]) == "true"
    except GitError:
        return False


def initialize_repository(workspace: str | Path) -> str:
    root = Path(workspace).expanduser().resolve()
    if is_repository(root):
        return f"已经是 Git 仓库：{root}"
    if not (root / ".gitignore").exists():
        (root / ".gitignore").write_text(DEFAULT_GITIGNORE, encoding="utf-8")
    result = run_git(root, ["init", "--initial-branch=main"])
    return f"已在 {root} 初始化 main 分支。\n{result}".strip()


def set_remote(workspace: str | Path, url: str, name: str = "origin") -> str:
    root = Path(workspace).expanduser().resolve()
    if not is_repository(root):
        raise GitError("当前工作空间还不是 Git 仓库，请先初始化或克隆仓库。")
    url = url.strip()
    if not re.fullmatch(r"(?:https?://|ssh://|git@)[^\s]+", url, re.IGNORECASE):
        raise GitError("请输入 HTTPS、SSH 或 git@host:path 格式的仓库地址。不要把访问令牌写进 URL。")
    names = run_git(root, ["remote"]).splitlines()
    if name in names:
        run_git(root, ["remote", "set-url", name, url])
        action = "已更新"
    else:
        run_git(root, ["remote", "add", name, url])
        action = "已添加"
    return f"{action}远端 {name}：{url}"


def configure_identity(workspace: str | Path, name: str, email: str) -> str:
    if not is_repository(workspace):
        raise GitError("当前工作空间还不是 Git 仓库，请先初始化或克隆仓库。")
    if not name.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()):
        raise GitError("请填写提交者姓名和有效邮箱。")
    run_git(workspace, ["config", "user.name", name.strip()])
    run_git(workspace, ["config", "user.email", email.strip()])
    return f"已为当前仓库设置提交身份：{name.strip()} <{email.strip()}>"


def git_config_value(workspace: str | Path, key: str) -> str:
    try:
        return run_git(workspace, ["config", "--get", key])
    except GitError:
        return ""


def clone_repository(url: str, destination: str | Path) -> str:
    url = url.strip()
    if not re.fullmatch(r"(?:https?://|ssh://|git@)[^\s]+", url, re.IGNORECASE):
        raise GitError("请输入 HTTPS、SSH 或 git@host:path 格式的仓库地址。不要把访问令牌写进 URL。")
    target = Path(destination).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise GitError(f"克隆目标目录必须为空：{target}")
    try:
        result = subprocess.run(
            ["git", "clone", url, str(target)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise GitError("找不到 Git for Windows；请安装后重启 Agent。") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("克隆超时。检查网络或仓库权限后重试。") from exc
    if result.returncode:
        raise GitError((result.stderr or result.stdout or "git clone 失败").strip())
    return f"已克隆到：{target}\n{(result.stdout or '').strip()}".strip()


def pull_fast_forward(workspace: str | Path) -> str:
    return run_git(workspace, ["pull", "--ff-only"], timeout=300)


def push_current_branch(workspace: str | Path) -> str:
    return run_git(workspace, ["push", "-u", "origin", "HEAD"], timeout=300)


def stage_and_commit(workspace: str | Path, message: str) -> str:
    if not message.strip():
        raise GitError("提交说明不能为空。")
    run_git(workspace, ["add", "-A"], timeout=120)
    return run_git(workspace, ["commit", "-m", message.strip()], timeout=120)
