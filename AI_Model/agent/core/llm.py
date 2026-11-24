"""LLM 提供者抽象层。

现在只有 Ollama 本地实现；以后要接别的模型 API，
新建一个类继承 LLMProvider，实现 chat() 即可，Agent 核心不用改。
"""
from __future__ import annotations

import json
import base64
import re
import time
import urllib.error
import urllib.request


class LLMProvider:
    """所有模型提供者的基类。

    返回统一的 dict: {"content": str, "tool_calls": [{"id","name","arguments"}], "raw": dict}
    """

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None, options: dict | None = None,
             on_token=None, on_progress=None, think: bool | None = None) -> dict:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError


def _normalize_args(args):
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _strict_args(args):
    """Parse tool arguments without silently turning malformed JSON into {}."""
    if args is None:
        return {}, None
    if isinstance(args, dict):
        return args, None
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (ValueError, TypeError) as exc:
            return None, f"工具参数不是有效 JSON：{exc}"
        if isinstance(parsed, dict):
            return parsed, None
        return None, "工具参数必须是 JSON 对象"
    return None, f"工具参数类型无效：{type(args).__name__}"


def normalize_raw_tool_message(message: dict) -> tuple[dict, list[str]]:
    """Return an Ollama-compatible assistant message, preserving tool metadata."""
    normalized = dict(message)
    errors = []
    calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            errors.append(f"第 {index + 1} 个工具调用格式无效")
            continue
        copied = dict(call)
        fn = dict(call.get("function") or {})
        args, error = _strict_args(fn.get("arguments", call.get("arguments")))
        if error:
            errors.append(f"{fn.get('name') or '未知工具'}：{error}")
        else:
            fn["arguments"] = args
        copied["function"] = fn
        copied.setdefault("index", index)
        calls.append(copied)
    if calls:
        normalized["tool_calls"] = calls
    return normalized, errors


def _extract_json(text: str):
    """从文本里抠出 JSON 对象/数组，兼容 ```json 包裹和前后废话。"""
    if not text:
        return None
    s = text.strip()
    # 去掉 markdown 代码块
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```", s, re.I)
    if m:
        s = m.group(1)
    # 找第一个 { 或 [ 到最后一个 } 或 ]
    start_candidates = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    end = max(s.rfind("}"), s.rfind("]"))
    if end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return None


def parse_tool_calls(message: dict) -> list[dict]:
    """统一工具调用格式。

    兼容两种情况：
    1. 标准 message.tool_calls（qwen3.5 等）
    2. content 里的 JSON 文本（qwen2.5-coder 等模板不标准时）
    """
    calls = []
    raw_calls = message.get("tool_calls") or []
    for i, c in enumerate(raw_calls):
        fn = c.get("function", {}) if isinstance(c, dict) else {}
        name = fn.get("name") or c.get("name")
        if not name:
            continue
        args, error = _strict_args(fn.get("arguments", c.get("arguments")))
        if error:
            continue
        calls.append({
            "id": c.get("id") or f"call_{i}_{int(time.time()*1000)}",
            "name": name,
            "arguments": args,
        })
    if calls:
        return calls

    # 兼容非标准输出：模型把工具调用写成 JSON 文本
    content = message.get("content") or ""
    parsed = _extract_json(content)
    if parsed is None:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        # 兼容多种模型命名：name/tool（qwen2.5-coder）、action（deepseek-r1）
        name = item.get("name") or item.get("tool") or item.get("action")
        # 也兼容 {"read_file": {"path": "..."}} 这种格式
        if not name and len(item) == 1:
            k, v = next(iter(item.items()))
            if isinstance(v, dict):
                name, args = k, v
            else:
                continue
        else:
            args = item.get("arguments") or item.get("args") or item.get("parameters") or {}
            args = {k: v for k, v in item.items()
                    if k not in ("name", "tool", "action", "arguments", "args", "parameters")} or args
        if not name:
            continue
        calls.append({
            "id": f"json_{i}_{int(time.time()*1000)}",
            "name": name,
            "arguments": _strict_args(args)[0] or {},
        })
    return calls


class OllamaProvider(LLMProvider):
    """通过 Ollama HTTP API 调用本地模型。"""

    def __init__(self, base_url: str = "http://127.0.0.1:11434",
                 default_model: str = "qwen2.5-coder:7b"):
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model

    def _post(self, path: str, payload: dict, timeout: int = 300) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama HTTP {e.code}: {body[:500]}") from e

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None, options: dict | None = None,
             on_token=None, on_progress=None, think: bool | None = None) -> dict:
        # Ollama accepts images as base64 strings on user messages.
        normalized_messages = []
        for message in messages:
            item = dict(message)
            paths = item.pop("image_paths", None)
            if paths:
                encoded = []
                for path in paths:
                    with open(path, "rb") as image_file:
                        encoded.append(base64.b64encode(image_file.read()).decode("ascii"))
                item["images"] = encoded
            normalized_messages.append(item)
        payload = {
            "model": model or self.default_model,
            "messages": normalized_messages,
            "stream": bool(on_token or on_progress),
            # A modest repetition penalty helps small local models avoid phrase loops.
            "options": options or {"temperature": 0.2, "num_predict": 3072,
                                    "repeat_penalty": 1.12, "repeat_last_n": 128,
                                    # Ollama otherwise often allocates only 4K on this 8 GB GPU.
                                    "num_ctx": 8192},
        }
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think
        if on_token or on_progress:
            req = urllib.request.Request(
                self.base_url + "/api/chat",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            content_parts, thinking_parts, tool_calls, resp = [], [], {}, {}
            json_candidate = None
            try:
                with urllib.request.urlopen(req, timeout=300) as response:
                    for line in response:
                        if not line.strip():
                            continue
                        chunk = json.loads(line.decode("utf-8"))
                        message = chunk.get("message") or {}
                        thinking = message.get("thinking") or ""
                        if thinking:
                            thinking_parts.append(thinking)
                            if on_progress:
                                on_progress(thinking)
                        piece = message.get("content") or ""
                        if piece:
                            content_parts.append(piece)
                            if json_candidate is not None:
                                json_candidate += piece
                            elif piece.lstrip().startswith(("{", "[")):
                                json_candidate = piece
                            else:
                                if on_token:
                                    on_token(piece)
                        for index, call in enumerate(message.get("tool_calls") or []):
                            key = call.get("index", index)
                            current = tool_calls.setdefault(key, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            current["id"] = call.get("id") or current["id"]
                            fn = call.get("function") or {}
                            if fn.get("name"):
                                current["function"]["name"] += fn["name"]
                            arguments = fn.get("arguments")
                            if isinstance(arguments, str):
                                current["function"]["arguments"] += arguments
                            elif isinstance(arguments, dict):
                                current["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
                        if chunk.get("done"):
                            resp = chunk
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Ollama HTTP {e.code}: {body[:500]}") from e
            msg = {"role": "assistant", "content": "".join(content_parts)}
            if thinking_parts:
                msg["thinking"] = "".join(thinking_parts)
            if tool_calls:
                msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
                msg, tool_call_errors = normalize_raw_tool_message(msg)
            else:
                tool_call_errors = []
            if json_candidate and not parse_tool_calls(msg) and on_token:
                on_token(json_candidate)
            resp["message"] = msg
        else:
            resp = self._post("/api/chat", payload)
            msg = resp.get("message", {})
            msg, tool_call_errors = normalize_raw_tool_message(msg)
        return {
            "content": msg.get("content") or "",
            "tool_calls": parse_tool_calls(msg),
            "raw_message": msg,   # provider 保留 metadata 并规范化 arguments 后的消息，供工具调用回灌
            "tool_call_errors": tool_call_errors,
            "raw": resp,
        }

    def list_models(self) -> list[str]:
        with urllib.request.urlopen(self.base_url + "/api/tags", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in data.get("models", [])]

    def model_capabilities(self, model: str) -> list[str]:
        """Return Ollama's declared capabilities, such as tools and vision."""
        data = self._post("/api/show", {"model": model}, timeout=20)
        return data.get("capabilities", [])

    def check(self) -> bool:
        try:
            self.list_models()
            return True
        except Exception:
            return False


class OpenAICompatibleProvider(LLMProvider):
    """预留：以后接 DeepSeek / OpenAI 等兼容 API 时用。

    用法（以后）：
        provider = OpenAICompatibleProvider(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-xxx",
            default_model="deepseek-chat",
        )
    Agent 核心完全不用改。
    """

    def __init__(self, base_url: str, api_key: str, default_model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None, options: dict | None = None) -> dict:
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if options:
            payload.update(options)
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function", {})
            calls.append({
                "id": c.get("id") or f"call_{i}",
                "name": fn.get("name"),
                "arguments": _normalize_args(fn.get("arguments")),
            })
        return {"content": msg.get("content") or "", "tool_calls": calls, "raw": data}

    def list_models(self) -> list[str]:
        req = urllib.request.Request(
            self.base_url + "/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m["id"] for m in data.get("data", [])]
