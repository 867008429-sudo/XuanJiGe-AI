#!/usr/bin/env python3
"""
Mock DeepSeek SSE 服务端（P0 spike 专用，无 LLM）

行为复刻真实 DeepSeek /chat/completions 流式接口的关键难点：
- tool_calls 的 function.arguments 被切成 1-2 字符的增量分片（真实 API 就这样）
- 决策逻辑：messages 最后一条是 tool 结果 → 流式返回最终文本；
  否则 → 流式返回 tool_calls 分片（agent 每个人类回合触发一次工具调用，不会死循环）

用法: python mock_deepseek_server.py <port>
就绪后向 stdout 打印 READY
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TOOL_NAME = "query_liunian"
TOOL_ARGS = {"year": 2027}
TOOL_ID = "call_mock_001"
FINAL_TEXT = "2027 丁未年，未为木库，天干丁火。此年十神以比肩劫财为显，交友耗财之象偏重，具体流月还需参看节气换令。"


def sse_line(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def build_toolcall_stream():
    """把 arguments JSON 切成 1-2 字符分片，复刻真实 API 的增量拼接难点"""
    args_str = json.dumps(TOOL_ARGS, ensure_ascii=False)
    fragments = [args_str[i:i + 2] for i in range(0, len(args_str), 2)]
    out = []
    # 首片：带 id/name/type
    out.append(sse_line(chunk({
        "role": "assistant", "content": None, "refusal": None,
        "tool_calls": [{
            "index": 0, "id": TOOL_ID, "type": "function",
            "function": {"name": TOOL_NAME, "arguments": ""},
        }],
    })))
    # 后续片：只有 index + arguments 增量（不带 id/name —— 这正是聚合难点）
    for frag in fragments:
        out.append(sse_line(chunk({
            "tool_calls": [{"index": 0, "function": {"arguments": frag}}],
        })))
    out.append(sse_line(chunk({}, finish="tool_calls")))
    return out


def build_text_stream():
    out = [sse_line(chunk({"role": "assistant", "content": ""}))]
    text = FINAL_TEXT
    for i in range(0, len(text), 4):
        out.append(sse_line(chunk({"content": text[i:i + 4]})))
    out.append(sse_line(chunk({}, finish="stop")))
    return out


def build_completion_json(want_text: bool) -> dict:
    """非流式响应（stream=False 的 invoke 路径）"""
    if want_text:
        msg = {"role": "assistant", "content": FINAL_TEXT}
        finish = "stop"
    else:
        msg = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": TOOL_ID, "type": "function",
                "function": {"name": TOOL_NAME, "arguments": json.dumps(TOOL_ARGS, ensure_ascii=False)},
            }],
        }
        finish = "tool_calls"
    return {
        "id": "chatcmpl-mock", "object": "chat.completion",
        "created": int(time.time()), "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = body.get("messages", [])
        last_role = messages[-1].get("role") if messages else None
        want_text = last_role == "tool"

        if body.get("stream", False):
            # 流式路径：SSE 分片（tool_calls arguments 切碎片）
            events = build_text_stream() if want_text else build_toolcall_stream()
            payload = b"".join(events) + b"data: [DONE]\n\n"
            ctype = "text/event-stream; charset=utf-8"
        else:
            # 非流式路径：完整 JSON（invoke 走这里）
            payload = json.dumps(build_completion_json(want_text), ensure_ascii=False).encode("utf-8")
            ctype = "application/json"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # 健康探测
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18777
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
