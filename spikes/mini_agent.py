#!/usr/bin/env python3
"""
P0 Mini Agent：LangGraph 图机制全链路验证（quickstart 验收件）

复刻 v4.1 第 4 节的目标图拓扑（缩小版）：
  START → context_gate → agent ⇄ tools → END

验证项：
  1. 图编译 + invoke 完整跑通（工具循环内由框架注入 runtime，无需手动传）
  2. SqliteSaver checkpointer：同 thread 第二次 invoke 累积消息（多轮记忆）
  3. thread 隔离：不同 thread_id 状态互不串扰（串盘防线）
  4. stream_mode="messages" 流式（gevent 下）
  5. context_gate 注入 digest 只在首回合生效
  6. recursion_limit 存在且可配
模型打 mock DeepSeek（协议与真实一致），引擎用真实 bazi_engine。
"""
import subprocess
import sys
import time
import datetime
from typing import Annotated, TypedDict

from gevent import monkey
monkey.patch_all()

import requests  # noqa: E402
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_deepseek import ChatDeepSeek  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根
import bazi_engine  # noqa: E402

DIGEST = "命盘摘要：乾造，日主甲木，生于午月。"


@tool
def query_liunian(year: int) -> str:
    """查询某一公历年的流年干支与十神（相对日主甲木）。"""
    dt = datetime.datetime(year, 6, 1, 12, 0)
    gan, zhi, mgan, mzhi = bazi_engine.get_exact_year_month_gz(dt)
    sh = bazi_engine.get_shishen_by_name("甲", gan)
    return f"{year}年 流年柱 {gan}{zhi}，年干对日主甲木十神 {sh}"


def wait_ready(port, timeout=10):
    for _ in range(timeout * 20):
        try:
            requests.get(f"http://127.0.0.1:{port}/", timeout=0.5)
            return True
        except Exception:
            time.sleep(0.05)
    return False


class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    paipan_digest: str


def make_graph(model, checkpointer):
    tools = [query_liunian]
    bound = model.bind_tools(tools)

    def context_gate(state: ChatState) -> dict:
        # spike 发现：add_messages reducer 只追加，system 会被排到 human 之后。
        # 正确做法：digest 存独立 state 字段（paipan_digest），agent 节点调用模型时前置
        if state.get("paipan_digest"):
            return {}
        return {"paipan_digest": DIGEST}

    def agent(state: ChatState) -> dict:
        # digest 每次调用时前置进 prompt，不进会话历史 → 永不重复、永不乱序
        msgs = [SystemMessage(content=state["paipan_digest"])] + state["messages"]
        return {"messages": [bound.invoke(msgs)]}

    builder = StateGraph(ChatState)
    builder.add_node("context_gate", context_gate)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "context_gate")
    builder.add_edge("context_gate", "agent")

    def route(state: ChatState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder.add_conditional_edges("agent", route, ["tools", END])
    builder.add_edge("tools", "agent")
    # 注：langgraph 1.x 中 recursion_limit 是运行时 config 参数（非 compile 参数）
    return builder.compile(checkpointer=checkpointer)


def main():
    results = []
    ok = lambda name, cond: results.append((name, bool(cond)))

    port = 18781
    server = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_deepseek_server.py"), str(port)]
    )
    dbp = "/tmp/spike_mini.db"
    if os.path.exists(dbp):
        os.remove(dbp)

    try:
        assert wait_ready(port)
        model = ChatDeepSeek(
            model="deepseek-v4-flash", api_key="sk-mock",
            api_base=f"http://127.0.0.1:{port}",
        )
        with SqliteSaver.from_conn_string(dbp) as saver:
            graph = make_graph(model, saver)

            t1 = "acctA:h101"
            t2 = "acctA:h102"  # 同账号换盘 → 必须隔离

            # ① 首回合（recursion_limit 走运行时 config，langgraph 1.x 用法）
            out1 = graph.invoke(
                {"messages": [HumanMessage("2027年我的流年如何？")]},
                config={"configurable": {"thread_id": t1}, "recursion_limit": 25},
            )
            msgs1 = out1["messages"]
            ok(f"首回合消息链完整（{len(msgs1)} 条：human+asst+tool+asst，system 不入历史）",
               len(msgs1) == 4 and all(m.type != "system" for m in msgs1))
            ok("context_gate 注入 digest 到独立字段", out1["paipan_digest"] == DIGEST)
            tool_msgs = [m for m in msgs1 if m.type == "tool"]
            ok(f"工具执行且输出丁未年: {tool_msgs[0].content[:30] if tool_msgs else None}",
               tool_msgs and "丁未" in tool_msgs[0].content)
            ok("最终答案含流年信息", "丁未" in msgs1[-1].content)

            # ② 同 thread 第二回合（checkpointer 记忆）
            out2 = graph.invoke(
                {"messages": [HumanMessage("那一年我该注意什么？")]},
                config={"configurable": {"thread_id": t1}},
            )
            msgs2 = out2["messages"]
            ok(f"第二回合累积（{len(msgs2)} 条 > 4，记忆生效）", len(msgs2) == 8)
            ok("digest 不重复（历史中 0 条 system，字段仍唯一）",
               all(m.type != "system" for m in msgs2) and out2["paipan_digest"] == DIGEST)

            # ③ thread 隔离
            out3 = graph.invoke(
                {"messages": [HumanMessage("2027年我的流年如何？")]},
                config={"configurable": {"thread_id": t2}},
            )
            msgs3 = out3["messages"]
            ok(f"新 thread 状态全新（{len(msgs3)} 条，不串 t1 的 8 条）", len(msgs3) == 4)

            # ④ get_state 检查点可读
            snap = graph.get_state(config={"configurable": {"thread_id": t1}})
            ok("checkpoint 快照可读", len(snap.values.get("messages", [])) == len(msgs2))

            # ⑤ stream_mode="messages" 流式（gevent 下）
            tokens = []
            for ev in graph.stream(
                {"messages": [HumanMessage("再说说丁未年")]},
                config={"configurable": {"thread_id": t2}},
                stream_mode="messages",
            ):
                msg, meta = ev if isinstance(ev, tuple) and len(ev) == 2 else (ev, {})
                if getattr(msg, "content", None) and not getattr(msg, "tool_call_chunks", None):
                    tokens.append(str(msg.content))
            ok(f"messages 流式出 token（{sum(len(t) for t in tokens)} 字符）",
               sum(len(t) for t in tokens) > 10)

    finally:
        server.terminate()
        server.wait(timeout=5)

    print("\n" + "=" * 56)
    print("Mini Agent 结果：图机制全链路 @ gevent + SqliteSaver")
    print("=" * 56)
    passed = sum(1 for _, c in results if c)
    for name, cond in results:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
