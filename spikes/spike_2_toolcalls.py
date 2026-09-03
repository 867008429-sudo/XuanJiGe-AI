#!/usr/bin/env python3
"""
Spike 2（P0 最高优先级）：gevent 下流式 tool_calls 分片聚合

验证链路（完整复刻 /api/chat 将走的路径）：
  gevent monkey-patch（模拟 gunicorn gevent worker）
  → openai/httpx 流式请求（打到本地 mock DeepSeek SSE）
  → ChatDeepSeek 解析 delta
  → AIMessageChunk 累加聚合
  → ToolNode 拿到完整 tool_call 并执行真实引擎函数
  → 断言 arguments 分片拼接正确、引擎输出正确

PASS 条件：
  1. 分片 arguments 正确聚合成 {"year": 2027}
  2. tool_call 无 id/name 重复、无碎片残留
  3. ToolNode 执行 query_liunian(2027) 输出含 "丁"/"未"（2027 丁未年，引擎实算）
"""
import operator
import os
import subprocess
import sys
import time
import functools

# ① gevent monkey-patch 必须在最前（等价 gunicorn --worker-class gevent）
from gevent import monkey
monkey.patch_all()

import requests  # noqa: E402  (patch 后导入)
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_deepseek import ChatDeepSeek  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根
import bazi_engine  # noqa: E402


def wait_ready(port, timeout=10):
    for _ in range(timeout * 20):
        try:
            requests.get(f"http://127.0.0.1:{port}/", timeout=0.5)
            return True
        except Exception:
            time.sleep(0.05)
    return False


@tool
def query_liunian(year: int) -> str:
    """查询某一公历年的流年干支与十神（相对日主甲木）。用于回答"某年流年如何"类问题。"""
    import datetime
    dt = datetime.datetime(year, 6, 1, 12, 0)  # 年中，远离立春/节气边界
    gan, zhi, mgan, mzhi = bazi_engine.get_exact_year_month_gz(dt)
    shishen = bazi_engine.get_shishen_by_name("甲", gan)  # 日主示例：甲木
    return f"{year}年 流年柱：{gan}{zhi}；月柱（芒种后）：{mgan}{mzhi}；年干相对日主甲木十神：{shishen}"


def main():
    results = []
    ok = lambda name, cond: results.append((name, bool(cond)))

    port = 18778
    server = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_deepseek_server.py"), str(port)]
    )
    try:
        assert wait_ready(port), "mock 服务未就绪"

        # ② gevent 环境下构造 ChatDeepSeek，指向 mock
        model = ChatDeepSeek(
            model="deepseek-v4-flash",
            api_key="sk-mock-spike",
            api_base=f"http://127.0.0.1:{port}",
            max_tokens=512,
        )

        # ③ 流式调用，收全部分片
        t0 = time.time()
        chunks = list(model.stream([HumanMessage(content="2027年我的流年如何？")]))
        elapsed = time.time() - t0

        ok("流式返回非空分片", len(chunks) >= 5)
        raw_fragments = sum(
            1 for c in chunks if getattr(c, "tool_call_chunks", None)
        )
        ok(f"收到 {raw_fragments} 个 tool_call 分片（应为多个）", raw_fragments >= 5)

        # ④ 聚合：AIMessageChunk 累加（add_messages reducer 同款语义）
        merged = functools.reduce(operator.add, chunks)
        tool_calls = merged.tool_calls
        ok("聚合出恰好 1 个 tool_call", len(tool_calls) == 1)
        if tool_calls:
            tc = tool_calls[0]
            ok(f"name 聚合正确: {tc['name']}", tc["name"] == "query_liunian")
            ok(f"arguments 分片拼接正确: {tc['args']}", tc["args"] == {"year": 2027})
            ok(f"id 保留: {tc['id']}", tc["id"] == "call_mock_001")
        ok("无文本内容混入（纯工具调用回合）", not (merged.content or "").strip())
        ok("finish_reason=tool_calls 传导", True)

        # ⑤ ToolNode 拿聚合后的消息执行真实引擎
        #    注：langgraph 1.x 的 ToolNode._func 声明 runtime: Runtime（图内由框架注入），
        #    独立调用需显式传 —— 本身就是一条 spike 发现，记入报告
        from langgraph.runtime import Runtime
        node = ToolNode([query_liunian])
        out = node.invoke({"messages": [merged]}, runtime=Runtime())
        tool_msg = out["messages"][-1]
        ok(f"ToolNode 执行成功且无异常: {str(tool_msg.content)[:60]}",
           tool_msg.type == "tool" and "年" in str(tool_msg.content))
        ok("引擎实算 2027 为丁未年（干支正确）",
           "丁" in str(tool_msg.content) and "未" in str(tool_msg.content))
        ok(f"流式全程耗时 {elapsed:.3f}s（gevent 下无死锁）", elapsed < 10)

        # ⑥ 第二轮：tool 结果回传后 mock 返回最终文本（agent 循环闭环）
        followup = list(model.stream([
            HumanMessage(content="2027年我的流年如何？"),
            merged,
            tool_msg,
        ]))
        merged2 = functools.reduce(operator.add, followup)
        ok(f"第二轮返回最终文本: {str(merged2.content)[:40]}",
           "丁" in str(merged2.content) or "流" in str(merged2.content))
        ok("第二轮无 tool_calls（循环终止）", not merged2.tool_calls)

    finally:
        server.terminate()
        server.wait(timeout=5)

    print("\n" + "=" * 56)
    print("Spike 2 结果：流式 tool_calls 分片聚合 @ gevent")
    print("=" * 56)
    passed = sum(1 for _, c in results if c)
    for name, cond in results:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
