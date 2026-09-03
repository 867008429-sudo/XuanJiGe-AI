#!/usr/bin/env python3
"""
Spike 4：模型路线定案 —— 双协议验证 + 真实端点可达性

按用户补充（"OpenAI 的 GPT 也可以用"）验证：
  A. ChatDeepSeek 路线（langchain-deepseek 1.1.0）→ mock 端点流式 tool_calls
  B. ChatOpenAI 路线（langchain-openai 1.6.0，openai_api_base 指向同一 mock）
     → 证明 OpenAI 兼容协议可互换：换 GPT 模型只改 model/base_url 两个字段
  C. 真实端点可达性：api.deepseek.com / api.openai.com（经代理）
  D. 版本锁定清单（requirements 追加项）

PASS 条件：A/B 各自完成一轮 tool_calls 流式聚合；C 至少 deepseek 可达
"""
import functools
import operator
import os
import subprocess
import sys
import time

from gevent import monkey
monkey.patch_all()

import requests  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_deepseek import ChatDeepSeek  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402


def wait_ready(port, timeout=10):
    for _ in range(timeout * 20):
        try:
            requests.get(f"http://127.0.0.1:{port}/", timeout=0.5)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def run_one_route(name, model):
    chunks = list(model.stream([HumanMessage(content="2027年我的流年如何？")]))
    merged = functools.reduce(operator.add, chunks)
    tcs = merged.tool_calls
    good = len(tcs) == 1 and tcs[0]["args"] == {"year": 2027}
    print(f"  [{name}] {'PASS' if good else 'FAIL'} "
          f"tool_calls={tcs[0]['args'] if tcs else None}")
    return good


def check_reachable(name, url):
    try:
        r = requests.get(url, timeout=8, proxies=None)  # 走显式代理变量由环境提供
        print(f"  [可达性:{name}] {r.status_code}")
        return True
    except Exception as e:
        # requests.get 默认会读环境代理；这里失败多为 404/401（能连上就算可达）
        try:
            r = requests.post(url, timeout=8, json={})
            print(f"  [可达性:{name}] HTTP {r.status_code}（可连通）")
            return r.status_code < 500
        except Exception as e2:
            print(f"  [可达性:{name}] 不可达: {type(e2).__name__}")
            return False


def main():
    results = []

    port = 18780
    server = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_deepseek_server.py"), str(port)]
    )
    try:
        assert wait_ready(port), "mock 服务未就绪"

        print("== A/B: 双模型路线打同一 OpenAI 兼容端点 ==")
        deep = ChatDeepSeek(
            model="deepseek-v4-flash",
            api_key="sk-mock",
            api_base=f"http://127.0.0.1:{port}",
        )
        results.append(("ChatDeepSeek 路线聚合", run_one_route("ChatDeepSeek 1.1.0", deep)))

        oai = ChatOpenAI(
            model="gpt-test",
            api_key="sk-mock",
            openai_api_base=f"http://127.0.0.1:{port}",
        )
        results.append(("ChatOpenAI 路线聚合", run_one_route("ChatOpenAI 1.6.0 ", oai)))

        print("\n== C: 真实端点可达性（沙箱经代理） ==")
        results.append(("api.deepseek.com 可达",
                        check_reachable("deepseek", "https://api.deepseek.com/")))
        results.append(("api.openai.com 可达",
                        check_reachable("openai", "https://api.openai.com/")))

        print("\n== D: 版本锁定清单（requirements.txt 追加） ==")
        import importlib.metadata as im
        pins = [f"{p}=={im.version(p)}" for p in [
            "langgraph", "langgraph-checkpoint-sqlite",
            "langchain-deepseek", "langchain-openai", "langchain-core"]]
        print("  " + "\n  ".join(pins))
        results.append(("版本清单产出", True))
    finally:
        server.terminate()
        server.wait(timeout=5)

    print("\n" + "=" * 56)
    print("Spike 4 结果：模型路线（DeepSeek / OpenAI 双协议）")
    print("=" * 56)
    passed = sum(1 for _, c in results if c)
    for name, cond in results:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
