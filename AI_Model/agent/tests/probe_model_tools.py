#!/usr/bin/env python3
"""逐个核实四个本地模型的 function calling 能力（真实 Ollama API）。

不启动 GUI，直接发 /api/chat 带 tools schema，检查返回是否：
1. 有标准 tool_calls 字段
2. 工具名/参数正确
结果写入 probe_result.json 供交接。
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OLLAMA = "http://127.0.0.1:11434"
MODELS = ["qwen3.5:4b", "deepseek-r1:8b", "qwen2.5-coder:7b", "qwen2.5vl:7b"]

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的当前天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}]


def probe(model: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "武汉现在天气怎么样？请调用 get_weather 工具查询。"}],
        "tools": TOOLS,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    started = datetime.now()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = (datetime.now() - started).total_seconds()
        msg = data.get("message", {})
        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "")[:300]
        thinking = (msg.get("thinking") or "")[:120]
        if calls:
            call = calls[0]
            fn = call.get("function", {})
            args = fn.get("arguments", {})
            ok = fn.get("name") == "get_weather" and isinstance(args, dict) and "city" in args
            return {
                "model": model, "status": "OK" if ok else "PARTIAL", "elapsed_sec": round(elapsed, 1),
                "tool_calls": len(calls), "called_name": fn.get("name"), "arguments": args,
                "content": content, "verdict": "标准 function calling 正常" if ok else "有 tool_calls 但参数/名称异常",
            }
        return {
            "model": model, "status": "NO_TOOL_CALL", "elapsed_sec": round(elapsed, 1),
            "tool_calls": 0, "content": content, "thinking": thinking,
            "verdict": "未返回标准 tool_calls（可能纯文本/JSON 或非标准格式，需要兼容层）",
        }
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        verdict = "模型不支持 tools（Ollama 明确返回 does not support tools）" if "does not support tools" in body else "HTTP 错误，需排查"
        return {"model": model, "status": "ERROR", "error": f"HTTP {exc.code}: {body}", "verdict": verdict}
    except Exception as exc:
        return {"model": model, "status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                "verdict": "调用失败，需排查"}


def main():
    # 先确认服务
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5)
    except Exception as exc:
        print(f"Ollama 服务不可用：{exc}")
        sys.exit(1)
    results = []
    for model in MODELS:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 测试 {model} ...", flush=True)
        r = probe(model)
        results.append(r)
        print(f"  → {r['status']}  {r.get('verdict', '')}  ({r.get('elapsed_sec', '?')}s)", flush=True)
    out = BASE / "probe_result.json"
    out.write_text(json.dumps({
        "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "endpoint": OLLAMA, "method": "/api/chat + tools schema", "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
