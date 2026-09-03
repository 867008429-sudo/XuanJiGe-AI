#!/usr/bin/env python3
"""
Spike 5：BGE 中文 embedding 本地跑（RAG 轨道前置验证）

路线：fastembed（ONNX 推理，无 torch 依赖，镜像 ~100MB 量级）
模型：BAAI/bge-small-zh-v1.5（512 维，中文，公版 Apache-2.0）

PASS 条件：
  1. 模型本地加载成功
  2. 中文文言句子向量化成功，维度 512
  3. 语义相似度正确：命理相关句 vs 无关句，相关对相似度显著更高
  4. 单条查询 embedding 延迟 < 50ms（CPU，R2 的查询侧性能门槛）
"""
import sys
import time

import numpy as np


def main():
    results = []
    ok = lambda name, cond: results.append((name, bool(cond)))

    try:
        from fastembed import TextEmbedding
    except ImportError:
        print("[SKIP] fastembed 未安装")
        return 2

    t0 = time.time()
    model = TextEmbedding("BAAI/bge-small-zh-v1.5")
    load_s = time.time() - t0
    ok(f"模型加载成功（{load_s:.1f}s）", load_s < 300)

    t1 = time.time()
    vecs = list(model.embed([
        "伤官见官，为祸百端",                      # 命理文言 A
        "伤官格喜佩印，忌见正官",                  # 命理文言 B（相关）
        "今天股市大盘走势如何",                    # 无关句
    ]))
    embed_ms = (time.time() - t1) * 1000
    ok("3 条中文句子向量化成功且维度 512", all(len(v) == 512 for v in vecs))

    v = [np.asarray(x) for x in vecs]
    cos = lambda a, b: float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    sim_rel = cos(v[0], v[1])   # 两句命理文言 → 应高
    sim_irr = cos(v[0], v[2])   # 命理 vs 股市 → 应低
    ok(f"语义判别正确：相关 {sim_rel:.3f} > 无关 {sim_irr:.3f}（差 > 0.05）",
       sim_rel - sim_irr > 0.05)

    t2 = time.time()
    _ = list(model.embed(["流年丁未比劫偏重"]))  # 查询侧单条
    q_ms = (time.time() - t2) * 1000
    ok(f"单条查询 embedding {q_ms:.1f}ms（< 50ms 门槛）", q_ms < 50)

    print("\n" + "=" * 56)
    print("Spike 5 结果：BGE-small-zh 本地 embedding（fastembed）")
    print("=" * 56)
    passed = sum(1 for _, c in results if c)
    for name, cond in results:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
