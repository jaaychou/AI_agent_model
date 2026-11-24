"""Small stdio MCP server for explicitly configured IMAP/SMTP mailboxes.

Credentials are read only from environment variables. The Agent GUI confirms each
MCP call, including send_email. This server supports plain-text mail only.
"""
from __future__ import annotations

import base64
from email import message_from_bytes
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.policy import default
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from email.message import EmailMessage
from email.utils import getaddresses
from datetime import datetime, timedelta, timezone
import imaplib
import json
import os
import re
import smtplib
import sys


def _config(prefix: str) -> dict:
    user = os.environ.get("EMAIL_USER", "").strip()
    try:
        import keyring
        password = keyring.get_password("LocalAIAgent/email", user) if user else None
    except Exception as exc:
        raise RuntimeError(f"无法访问 Windows 凭据保管库：{exc}") from exc
    return {
        "host": os.environ.get(prefix + "_HOST", "").strip(),
        "port": int(os.environ.get(prefix + "_PORT", "993" if prefix == "EMAIL_IMAP" else "465")),
        "user": user,
        "password": password or "",
    }


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


class _HTMLTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextParser()
    parser.feed(value)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", "".join(parser.parts))).strip()


def _internal_date(response: bytes) -> str:
    match = re.search(rb'INTERNALDATE\s+"([^"]+)"', response, re.IGNORECASE)
    if not match:
        return ""
    try:
        return parsedate_to_datetime(match.group(1).decode("ascii")).isoformat()
    except (ValueError, TypeError, UnicodeDecodeError):
        return ""


def _internal_date_key(response: bytes) -> float:
    value = _internal_date(response)
    if not value:
        return float("-inf")
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return float("-inf")


def encode_imap_utf7(value: str) -> str:
    """Encode mailbox names using RFC 3501 modified UTF-7 (ASCII-safe for IMAP)."""
    output = []
    pending = []

    def flush_pending():
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            output.append("&" + encoded + "-")
            pending.clear()

    for char in value:
        code = ord(char)
        if 0x20 <= code <= 0x7E:
            flush_pending()
            output.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush_pending()
    return "".join(output)


def send_netease_imap_id(client) -> None:
    """Send the IMAP ID extension required by NetEase mail servers."""
    # Python's imaplib has no public ID command; _simple_command is the narrow
    # stdlib hook needed to send this fixed RFC 2971 identity payload.
    # The stdlib command-state table does not register ID, so authorize it in
    # the authenticated states before calling _simple_command (otherwise
    # imaplib raises KeyError before sending anything).
    imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
    identity = '("name" "Local AI Agent" "version" "1.0" "vendor" "AI_Model_agent")'
    status, response = client._simple_command("ID", identity)
    if status != "OK":
        raise RuntimeError(f"网易 IMAP ID 身份声明失败（{status}: {response!r}）。")


def _text_body(message) -> str:
    if message.is_multipart():
        parts = [part for part in message.walk()
                 if part.get_content_type() in {"text/plain", "text/html"}
                 and part.get_content_disposition() != "attachment"]
        part = next((p for p in parts if p.get_content_type() == "text/plain"), None)
        if part is None:
            part = next((p for p in parts if p.get_content_type() == "text/html"), None)
    else:
        part = message
    if part is None:
        return ""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")[:12000]
    return _html_to_text(text) if part.get_content_type() == "text/html" else text


def list_recent_emails(arguments: dict) -> str:
    cfg = _config("EMAIL_IMAP")
    if not all((cfg["host"], cfg["user"], cfg["password"])):
        raise RuntimeError("请配置 EMAIL_IMAP_HOST、EMAIL_USER 和 EMAIL_APP_PASSWORD 环境变量。")
    folder = str(arguments.get("folder", "INBOX"))
    if not folder or any(ch in folder for ch in "\r\n\x00"):
        raise ValueError("邮箱文件夹名称无效。")
    limit = max(1, min(int(arguments.get("limit", 10)), 25))
    unread = bool(arguments.get("unread_only", True))
    include_body = bool(arguments.get("include_body", True))
    try:
        received_after = datetime.fromisoformat(arguments["received_after"]).astimezone() if arguments.get("received_after") else None
        received_before = datetime.fromisoformat(arguments["received_before"]).astimezone() if arguments.get("received_before") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("收件时间范围必须使用 ISO 8601 日期时间。") from exc
    client = imaplib.IMAP4_SSL(cfg["host"], cfg["port"], timeout=20)
    try:
        client.login(cfg["user"], cfg["password"])
        if cfg["host"].casefold().rstrip(".") in {"imap.163.com", "imap.126.com", "imap.188.com"}:
            send_netease_imap_id(client)
        status, _ = client.select(encode_imap_utf7(folder), readonly=True)
        if status != "OK":
            response = " ".join(str(part) for part in _)
            available = ""
            try:
                list_status, folders = client.list()
                if list_status == "OK" and folders:
                    available = "；服务器返回的文件夹：" + " | ".join(
                        part.decode("utf-8", errors="replace") if isinstance(part, bytes) else str(part)
                        for part in folders[:20]
                    )
            except Exception:
                pass
            raise RuntimeError(f"无法打开邮箱文件夹 {folder!r}（IMAP {status}: {response[:500]}）。{available}")
        criteria = ["UNSEEN" if unread else "ALL"]
        if received_after:
            criteria.extend(["SINCE", received_after.strftime("%d-%b-%Y")])
        if received_before:
            # IMAP BEFORE is exclusive by date; add one day, then enforce the
            # exact timestamp bounds locally using INTERNALDATE.
            criteria.extend(["BEFORE", (received_before.date() + timedelta(days=1)).strftime("%d-%b-%Y")])
        status, data = client.search(None, *criteria)
        if status != "OK":
            raise RuntimeError("邮箱搜索失败。")
        ids = data[0].split() if data and data[0] else []
        # IMAP SEARCH order is sequence order, not guaranteed chronological.
        # Rank a bounded window using server arrival time (INTERNALDATE),
        # which is the appropriate meaning of “latest” for a mailbox.
        candidates = ids[-min(len(ids), 100):]
        if candidates:
            rank_status, rank_rows = client.fetch(
                b",".join(candidates),
                "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
            )
            if rank_status == "OK":
                ranked = []
                for entry in rank_rows:
                    if not isinstance(entry, tuple):
                        continue
                    match = re.match(rb"\s*(\d+)", entry[0])
                    if match:
                        ranked.append((_internal_date_key(entry[0]), int(match.group(1))))
                if received_after or received_before:
                    after_ts = received_after.timestamp() if received_after else float("-inf")
                    before_ts = received_before.timestamp() if received_before else float("inf")
                    ranked = [(timestamp, seq) for timestamp, seq in ranked if after_ts <= timestamp < before_ts]
                if ranked:
                    ids = [str(seq).encode("ascii") for _date, seq in sorted(ranked)]
                elif received_after or received_before:
                    ids = []
        ids = ids[:limit]
        rows = []
        for uid in reversed(ids):
            # Bound body downloads so a large message/attachment cannot hold the
            # GUI at 95% indefinitely. IMAP partial FETCH is specified in RFC 3501.
            fetch_query = "(INTERNALDATE BODY.PEEK[]<0.32768>)" if include_body else "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            status, fetched = client.fetch(uid, fetch_query)
            if status != "OK":
                continue
            raw = next((entry[1] for entry in fetched if isinstance(entry, tuple)), None)
            if raw is None:
                continue
            message = message_from_bytes(raw)
            row = {
                "uid": uid.decode("ascii", errors="ignore"),
                "from": _decode_header(message.get("From")),
                "subject": _decode_header(message.get("Subject")),
                "date": message.get("Date", ""),
                "received": _internal_date(fetched[0][0]) if fetched and isinstance(fetched[0], tuple) else "",
            }
            if include_body:
                row["body"] = _text_body(message)
            rows.append(row)
        return json.dumps(rows, ensure_ascii=False)
    finally:
        try:
            client.logout()
        except Exception:
            pass


def send_email(arguments: dict) -> str:
    cfg = _config("EMAIL_SMTP")
    if not all((cfg["host"], cfg["user"], cfg["password"])):
        raise RuntimeError("请配置 EMAIL_SMTP_HOST、EMAIL_USER 和 EMAIL_APP_PASSWORD 环境变量。")
    recipients = [addr for _name, addr in getaddresses([str(arguments.get("to", ""))]) if addr]
    if not recipients or any("\r" in addr or "\n" in addr for addr in recipients):
        raise ValueError("请提供有效的收件人邮箱。")
    subject = str(arguments.get("subject", ""))[:300]
    body = str(arguments.get("body", ""))[:50000]
    if not subject.strip() or not body.strip():
        raise ValueError("主题和正文不能为空。")
    message = EmailMessage()
    message["From"] = cfg["user"]
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20) as client:
        client.login(cfg["user"], cfg["password"])
        client.send_message(message)
    return f"邮件已由 SMTP 服务接受，收件人：{', '.join(recipients)}。"


TOOLS = [
    {"name": "list_recent_emails", "description": "读取授权邮箱最近邮件；默认仅未读，不会标记已读。每次调用需用户确认。",
     "inputSchema": {"type": "object", "properties": {
         "folder": {"type": "string", "description": "邮箱文件夹，默认 INBOX"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 25},
         "unread_only": {"type": "boolean", "default": True},
         "include_body": {"type": "boolean", "default": True,
                          "description": "读取正文；若只需要发件人/主题/日期请设为 false，响应更快"},
         "received_after": {"type": "string", "description": "仅返回此 ISO 8601 时间之后收到的邮件"},
         "received_before": {"type": "string", "description": "仅返回此 ISO 8601 时间之前收到的邮件"}}, "additionalProperties": False}},
    {"name": "send_email", "description": "发送一封纯文本邮件；Agent GUI 会在发送前逐次显示工具调用确认。",
     "inputSchema": {"type": "object", "properties": {
         "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
         "required": ["to", "subject", "body"], "additionalProperties": False}},
]


def handle(request: dict) -> dict | None:
    method, params = request.get("method"), request.get("params") or {}
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "local-email-mcp", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        try:
            if name == "list_recent_emails":
                output = list_recent_emails(params.get("arguments") or {})
            elif name == "send_email":
                output = send_email(params.get("arguments") or {})
            else:
                raise ValueError("未知邮箱工具")
            result = {"content": [{"type": "text", "text": output}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stderr.write(f"email MCP request failed: {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
