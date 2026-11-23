"""通过 ComfyUI API 调用本地 Z-Image-Turbo 文生图。

用法示例：
  python zimage_generate.py "一只戴宇航头盔的猫, 电影感光照" --width 768 --height 768
生成结果保存在 ComfyUI/output 目录。
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

SERVER = "http://127.0.0.1:8188"
MAX_SIDE = 1024          # 硬上限：8GB 显存不要超过这个边长
MAX_STEPS = 12           # Turbo 模型 8 步足够，别无脑拉高
MIN_FREE_RAM_MB = 1400   # 可用内存低于此值直接拒绝提交
MAX_GPU_MEM_PCT = 85     # 显存超过此值直接拒绝提交


def emit_progress(stage: str, percent: int, elapsed: float = 0,
                  remaining: float | None = None):
    """Machine-readable progress consumed by the Agent GUI; ordinary CLI logs stay readable."""
    print("AGENT_PROGRESS:" + json.dumps({
        "stage": stage, "percent": max(0, min(100, int(percent))),
        "elapsed": max(0, int(elapsed)),
        "remaining": None if remaining is None else max(0, int(remaining)),
    }, ensure_ascii=False), flush=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "guard"))
try:
    import resource_guard as guard
except ImportError:
    guard = None


def preflight(width: int, height: int, steps: int) -> bool:
    """提交前检查资源和参数上限，防止把系统压死。"""
    if max(width, height) > MAX_SIDE:
        print("拒绝提交：边长上限 %d，当前 %dx%d" % (MAX_SIDE, width, height))
        return False
    if steps > MAX_STEPS:
        print("拒绝提交：步数上限 %d，当前 %d" % (MAX_STEPS, steps))
        return False
    if guard is None:
        return True
    m = guard.sample()
    gpu = m["gpu"]
    if m["free_ram_mb"] < MIN_FREE_RAM_MB:
        print("拒绝提交：可用内存仅 %.0fMB，低于安全线 %.0fMB" %
              (m["free_ram_mb"], MIN_FREE_RAM_MB))
        return False
    if gpu["available"] and gpu["used_pct"] > MAX_GPU_MEM_PCT:
        print("拒绝提交：显存已占用 %.0f%%，高于安全线 %d%%" %
              (gpu["used_pct"], MAX_GPU_MEM_PCT))
        return False
    print("预检通过：可用内存 %.0fMB，显存 %.0f%%，温度 %.0fC" %
          (m["free_ram_mb"], gpu["used_pct"], gpu["temp_c"]), flush=True)
    return True


def post_json(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SERVER + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path):
    with urllib.request.urlopen(SERVER + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_workflow(prompt, width, height, steps, seed):
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "z_image_turbo_int8_convrot.safetensors",
                "weight_dtype": "default",
            },
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "qwen_3_4b_fp8_mixed.safetensors",
                "type": "lumina2",
                "device": "default",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "ae.safetensors"},
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["2", 0]},
        },
        "5": {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["4", 0]},
        },
        "6": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "res_multistep",
                "scheduler": "sgm_uniform",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["7", 0], "vae": ["3", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "ZImage", "images": ["8", 0]},
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?", default="一只戴宇航头盔的猫，电影感光照，超写实")
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    emit_progress("检查生图资源", 3, 0, 90)
    if not preflight(args.width, args.height, args.steps):
        return 1

    client_id = str(uuid.uuid4())
    workflow = build_workflow(args.prompt, args.width, args.height, args.steps, args.seed)
    result = post_json("/prompt", {"prompt": workflow, "client_id": client_id})
    if "errors" in result:
        print("提交失败:", json.dumps(result, ensure_ascii=False))
        return 1

    prompt_id = result["prompt_id"]
    print("任务已提交 prompt_id =", prompt_id, flush=True)

    started = time.time()
    estimated_total = max(20.0, 34.0 * (args.width * args.height * args.steps) / (512 * 512 * 8))
    emit_progress("ComfyUI 生图中", 20, 0, estimated_total)
    while time.time() - started < args.timeout:
        history = get_json("/history/" + prompt_id)
        if prompt_id in history:
            entry = history[prompt_id]
            if entry.get("status", {}).get("status_str") == "error":
                print("生成失败:", json.dumps(entry, ensure_ascii=False)[:3000])
                return 2
            outputs = entry.get("outputs", {})
            images = []
            for node_out in outputs.values():
                images.extend(node_out.get("images", []))
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "ComfyUI", "output",
            )
            elapsed = time.time() - started
            print("生成完成，用时 %.1f 秒" % elapsed, flush=True)
            emit_progress("图片完成", 100, elapsed, 0)
            for img in images:
                path = os.path.join(output_dir, img.get("subfolder", ""), img["filename"])
                print("IMAGE_PATH:", path, flush=True)
            return 0
        elapsed = time.time() - started
        fraction = min(0.98, elapsed / estimated_total)
        # ComfyUI's history endpoint reports the final result only. The GUI
        # therefore labels this time-based fraction as an estimate, not a true step count.
        percent = min(95, 20 + int(fraction * 75))
        emit_progress("ComfyUI 生图中（估算）", percent, elapsed,
                      max(0, estimated_total - elapsed))
        time.sleep(3)

    print("等待超时（%.0f 秒），ComfyUI 可能仍在生成" % args.timeout)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
