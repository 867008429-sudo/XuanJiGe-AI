#!/usr/bin/env python3
"""
Spike 1：gevent monkey-patch 下 Flask + SSE 流式

复刻生产形态：gunicorn --worker-class gevent 的 monkey-patch 语义
  → Flask 路由返回生成器（逐 token yield）
  → gevent.pywsgi.WSGIServer 提供服务
  → 客户端流式增量消费

PASS 条件：
  1. Content-Type 为 text/event-stream
  2. 逐块增量到达（首块时间 << 总耗时，证明是流不是攒齐）
  3. 所有 token 有序完整
  4. 与图流式（stream_mode="messages"）风格一致的 data: 前缀格式
"""
import json
import sys
import time

from gevent import monkey
monkey.patch_all()

import requests  # noqa: E402
from flask import Flask, Response  # noqa: E402
from gevent.pywsgi import WSGIServer  # noqa: E402
import gevent  # noqa: E402

app = Flask(__name__)
TOKENS = ["丁", "未", "流", "年", "，", "比", "劫", "偏", "重", "。"]


@app.route("/api/chat", methods=["POST"])
def api_chat():
    def gen():
        for i, tok in enumerate(TOKENS):
            yield f"data: {json.dumps({'t': tok, 'i': i}, ensure_ascii=False)}\n\n"
            gevent.sleep(0.12)  # 模拟 LLM 出字间隔；gevent 下应为协作让出
        yield "data: [DONE]\n\n"

    return Response(gen(), mimetype="text/event-stream")


def main():
    results = []
    ok = lambda name, cond: results.append((name, bool(cond)))

    server = WSGIServer(("127.0.0.1", 18779), app, log=None)
    gevent.spawn(server.serve_forever)
    gevent.sleep(0.3)  # 等监听就绪

    t0 = time.time()
    first_at = None
    got = []
    with requests.post(
        "http://127.0.0.1:18779/api/chat", json={"q": "test"}, stream=True, timeout=30
    ) as r:
        ok(f"Content-Type: {r.headers.get('Content-Type', '')[:30]}",
           "text/event-stream" in r.headers.get("Content-Type", ""))
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data: ") and line != "data: [DONE]":
                if first_at is None:
                    first_at = time.time()
                got.append(json.loads(line[6:])["t"])
    total = time.time() - t0

    ok(f"token 有序完整（{len(got)}/{len(TOKENS)}）", got == TOKENS)
    ok(f"增量到达：首块 {first_at - t0:.3f}s << 总耗时 {total:.3f}s",
       first_at and (first_at - t0) < total * 0.5)
    ok("总耗时符合出字节奏（≈0.12s×10，未被阻塞成一次性返回）",
       total > 0.8)

    server.stop()

    print("\n" + "=" * 56)
    print("Spike 1 结果：gevent + Flask SSE 流式")
    print("=" * 56)
    passed = sum(1 for _, c in results if c)
    for name, cond in results:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
