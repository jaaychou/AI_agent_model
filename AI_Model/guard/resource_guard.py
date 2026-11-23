"""本地 AI 资源守护程序 —— 防止生图/大模型推理把系统和显卡撑爆。

监控：可用内存、CPU 占用、显存占用、GPU 温度。
分级动作：
  SOFT     -> 降低相关进程优先级、限制 CPU 核心数（不中断任务）
  HARD     -> 中断 ComfyUI 当前生成 + 卸载模型释放显存/内存
  CRITICAL -> 终止 ComfyUI 进程，避免系统硬卡死或显卡引擎超时蓝屏

用法：
  python resource_guard.py --once          # 只看当前指标，不动作
  python resource_guard.py --dry-run       # 常驻监控，只记录不动作
  python resource_guard.py                 # 常驻监控并自动限流
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import winsound
from datetime import datetime
from pathlib import Path

import psutil

BASE = Path(__file__).resolve().parent.parent
GUARD_DIR = BASE / "guard"
LOG_FILE = GUARD_DIR / "guard.log"
EVENT_FILE = GUARD_DIR / "guard_events.jsonl"
CONFIG_FILE = GUARD_DIR / "guard_config.json"
COMFY_URL = "http://127.0.0.1:8188"

DEFAULT_CONFIG = {
    "check_interval_seconds": 2,
    "hits_before_soft": 3,
    "hits_before_hard": 2,
    "cooldown_seconds": 45,
    "min_free_ram_mb_soft": 2200,
    "min_free_ram_mb_hard": 1400,
    "min_free_ram_mb_critical": 900,
    "gpu_mem_percent_soft": 80,
    "gpu_mem_percent_hard": 90,
    "gpu_mem_percent_critical": 96,
    "gpu_temp_soft_c": 82,
    "gpu_temp_hard_c": 88,
    "gpu_temp_critical_c": 92,
    "cpu_percent_soft": 92,
    "cpu_hits_before_soft": 15,
    "cpu_core_limit_ratio": 0.5,
    "beep_on_action": True,
    "watch_comfy": True,
    "watch_ollama": True,
    "watch_webui": True,
}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(level: str, msg: str) -> None:
    line = f"[{now()}] [{level}] {msg}"
    print(line, flush=True)
    try:
        GUARD_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def event(kind: str, metrics: dict, action: str) -> None:
    rec = {"time": now(), "level": kind, "action": action, "metrics": metrics}
    try:
        with EVENT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(user_cfg)
        except (OSError, ValueError) as exc:
            log("WARN", f"配置文件读取失败，使用默认值: {exc}")
    return cfg


def save_default_config() -> None:
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log("INFO", f"已生成默认配置: {CONFIG_FILE}")


def beep(times: int) -> None:
    try:
        for _ in range(times):
            winsound.Beep(880, 160)
            time.sleep(0.12)
    except (OSError, RuntimeError):
        pass


def gpu_metrics() -> dict:
    result = {"available": False, "used_mb": 0, "total_mb": 0, "used_pct": 0.0,
              "temp_c": 0.0, "util_pct": 0.0}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return result
        first = out.stdout.strip().splitlines()[0]
        used, total, temp, util = [float(x.strip()) for x in first.split(",")[:4]]
        result.update(available=True, used_mb=used, total_mb=total, temp_c=temp,
                      util_pct=util, used_pct=100.0 * used / max(total, 1))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def sample() -> dict:
    mem = psutil.virtual_memory()
    gpu = gpu_metrics()
    return {
        "free_ram_mb": mem.available / (1024 * 1024),
        "ram_used_pct": mem.percent,
        "cpu_pct": psutil.cpu_percent(interval=None),
        "gpu": gpu,
    }


def find_targets(cfg: dict) -> list[psutil.Process]:
    rules = []
    if cfg.get("watch_comfy", True):
        rules.append(("comfy", ["main.py", "--port", "8188"]))
    if cfg.get("watch_ollama", True):
        rules.append(("ollama", ["ollama.exe", "serve"]))
    if cfg.get("watch_webui", True):
        rules.append(("webui", ["open-webui.exe", "serve"]))
    found = []
    self_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        info = proc.info
        if not info.get("pid") or info["pid"] == self_pid:
            continue
        cmd = " ".join(info.get("cmdline") or [])
        low = cmd.lower()
        for role, keywords in rules:
            if all(k.lower() in low for k in keywords):
                found.append((role, proc))
                break
    return found


def post_comfy(path: str, payload: dict | None = None) -> bool:
    try:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            COMFY_URL + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=4):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def throttle(targets: list[tuple[str, psutil.Process]], cfg: dict) -> None:
    """轻柔限流：降优先级 + 限制核心数，不打断任务。"""
    cores = os.cpu_count() or 4
    limit = max(2, int(cores * float(cfg.get("cpu_core_limit_ratio", 0.5))))
    cpu_map = list(range(limit))
    for role, proc in targets:
        if role == "webui":
            continue
        try:
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except (psutil.Error, AttributeError):
            pass
        try:
            proc.cpu_affinity(cpu_map)
        except (psutil.Error, NotImplementedError):
            pass
        log("SOFT", f"限流 {role} pid={proc.pid} -> 低优先级 / CPU 限制在 {limit} 核")


def interrupt_generation(cfg: dict) -> None:
    if not cfg.get("watch_comfy", True):
        return
    if post_comfy("/interrupt", {}):
        log("HARD", "已向 ComfyUI 发送 /interrupt，当前生成任务安全中止")
    time.sleep(1)
    if post_comfy("/free", {"unload_models": True, "free_memory": True}):
        log("HARD", "已调用 ComfyUI /free，卸载模型释放显存和内存")


def rescue(targets: list[tuple[str, psutil.Process]], cfg: dict) -> None:
    """硬性救援：先限流，再中断生成并卸载模型。"""
    throttle(targets, cfg)
    interrupt_generation(cfg)


def emergency(targets: list[tuple[str, psutil.Process]], cfg: dict) -> None:
    """最后防线：终止 ComfyUI，防止系统卡死或显卡超时蓝屏。"""
    interrupt_generation(cfg)
    for role, proc in targets:
        if role == "comfy":
            try:
                proc.terminate()
                log("CRITICAL", f"已终止 ComfyUI pid={proc.pid}，系统资源已释放")
            except psutil.Error:
                pass
        elif role == "ollama":
            try:
                proc.nice(psutil.IDLE_PRIORITY_CLASS)
                proc.cpu_affinity([0, 1])
                log("CRITICAL", "Ollama 已降至最低优先级并限制 2 核")
            except (psutil.Error, AttributeError, NotImplementedError):
                pass


def evaluate(metrics: dict, cfg: dict, hits: dict) -> tuple[str | None, str]:
    gpu = metrics["gpu"]
    reasons = []

    critical = metrics["free_ram_mb"] < cfg["min_free_ram_mb_critical"]
    if gpu["available"]:
        critical |= gpu["used_pct"] >= cfg["gpu_mem_percent_critical"]
        critical |= gpu["temp_c"] >= cfg["gpu_temp_critical_c"]
    if critical:
        hits["critical"] = hits.get("critical", 0) + 1
        if hits["critical"] >= 1:
            return "CRITICAL", "资源已到临界值（内存/显存/温度），立即保护系统"
    else:
        hits["critical"] = 0

    hard = metrics["free_ram_mb"] < cfg["min_free_ram_mb_hard"]
    if gpu["available"]:
        hard |= gpu["used_pct"] >= cfg["gpu_mem_percent_hard"]
        hard |= gpu["temp_c"] >= cfg["gpu_temp_hard_c"]
    if hard:
        hits["hard"] = hits.get("hard", 0) + 1
        if hits["hard"] >= cfg["hits_before_hard"]:
            return "HARD", "资源持续高位，中断生成并释放显存"
    else:
        hits["hard"] = 0

    soft = metrics["free_ram_mb"] < cfg["min_free_ram_mb_soft"]
    if gpu["available"]:
        soft |= gpu["used_pct"] >= cfg["gpu_mem_percent_soft"]
        soft |= gpu["temp_c"] >= cfg["gpu_temp_soft_c"]
    if soft:
        hits["soft"] = hits.get("soft", 0) + 1
        if hits["soft"] >= cfg["hits_before_soft"]:
            return "SOFT", "资源偏高，执行预防性限流"
    else:
        hits["soft"] = 0

    if metrics["cpu_pct"] >= cfg["cpu_percent_soft"]:
        hits["cpu"] = hits.get("cpu", 0) + 1
        if hits["cpu"] >= cfg["cpu_hits_before_soft"]:
            return "SOFT", "CPU 持续满载，执行预防性限流"
    else:
        hits["cpu"] = 0

    if soft:
        reasons.append("资源偏高但尚未达到限流持续次数")
    return None, "; ".join(reasons)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只输出一次当前指标")
    parser.add_argument("--dry-run", action="store_true", help="只记录不动作")
    parser.add_argument("--interval", type=float, default=None, help="检查间隔秒数")
    args = parser.parse_args()

    save_default_config()
    cfg = load_config()
    if args.interval:
        cfg["check_interval_seconds"] = args.interval

    if args.once:
        print(json.dumps(sample(), ensure_ascii=False, indent=2))
        return 0

    log("INFO", "资源守护启动，阈值见 guard_config.json" + ("（DRY-RUN 只记录）" if args.dry_run else ""))
    hits: dict[str, int] = {}
    last_action = 0.0
    last_soft_log = 0.0

    while True:
        try:
            metrics = sample()
            level, reason = evaluate(metrics, cfg, hits)
            now_ts = time.time()

            if level:
                cooldown_ok = now_ts - last_action >= cfg["cooldown_seconds"]
                if level == "CRITICAL" or cooldown_ok:
                    targets = find_targets(cfg)
                    log(level, f"{reason} | 空闲内存={metrics['free_ram_mb']:.0f}MB "
                               f"CPU={metrics['cpu_pct']:.0f}% "
                               f"显存={metrics['gpu']['used_pct']:.0f}% "
                               f"GPU温度={metrics['gpu']['temp_c']:.0f}C")
                    if not args.dry_run:
                        if level == "SOFT":
                            throttle(targets, cfg)
                        elif level == "HARD":
                            rescue(targets, cfg)
                        else:
                            emergency(targets, cfg)
                        if cfg.get("beep_on_action", True):
                            beep(3 if level == "CRITICAL" else 2)
                    event(level, metrics, reason)
                    last_action = now_ts
                    hits = {}
                elif now_ts - last_soft_log > 30:
                    log("INFO", f"触发条件 {level} 但处于冷却期，跳过")
                    last_soft_log = now_ts
            time.sleep(cfg["check_interval_seconds"])
        except KeyboardInterrupt:
            log("INFO", "资源守护已停止")
            return 0
        except Exception as exc:  # 守护自身绝不能崩
            log("WARN", f"守护循环异常（已忽略）: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
