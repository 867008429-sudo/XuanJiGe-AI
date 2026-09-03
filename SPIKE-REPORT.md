# P0 Spike 验证报告

> 对应方案：`agent-plan.md` v4.1 第 4 节（P0 技术验证）
> 基线仓库：main @ 981e880（Flask + gunicorn 2x gevent + SQLite + DeepSeek）
> 结论先行：**Go —— 6 项验证全绿（39/40 断言通过），P1 可以开工。**
> 唯一未过项（OpenAI 域名连通）为沙箱出口策略限制，非工程问题，协议层已用等价方式验证，见 §5。

---

## 1. 验证矩阵

| # | 验证件 | 目的 | 结果 |
|---|--------|------|------|
| 1 | `spikes/spike_1_sse.py` | gevent monkey-patch 下 Flask SSE 流式不被阻塞 | **4/4 PASS** |
| 2 | `spikes/spike_2_toolcalls.py` | 流式 tool_calls 分片聚合 + ToolNode 执行真实引擎 | **13/13 PASS** |
| 3 | `spikes/spike_3_wal.py` | SQLite WAL 并发 + 配额原子扣减（防双花/退款） | **5/5 PASS** |
| 4 | `spikes/spike_4_model_route.py` | DeepSeek / OpenAI 双协议路线 + 真实端点可达 | **4/5**（见 §5） |
| 5 | `spikes/spike_5_embed.py` | BGE-small-zh 本地 embedding（fastembed/ONNX） | **4/4 PASS** |
| 6 | `spikes/mini_agent.py` | LangGraph 图 + checkpointer + thread 隔离全链路 | **9/9 PASS** |
| — | `spikes/mock_deepseek_server.py` | 1-6 共用的 OpenAI 兼容 mock 端点 | 支撑件 |

运行方式（复现）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install gevent flask requests numpy \
    langgraph==1.2.11 langgraph-checkpoint-sqlite==3.1.1 \
    langchain-core==1.6.1 langchain-deepseek==1.1.0 langchain-openai==1.6.0 \
    fastembed==0.8.0
cd spikes
python spike_1_sse.py        # 依次运行 1-5
python mini_agent.py
# spike_5 首次运行需走 HF 镜像下载模型（~100MB，之后走本地缓存）：
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 python spike_5_embed.py
```

`mini_agent.py` / `spike_2` 通过自身路径推导仓库根，直接 `import bazi_engine`，在仓库内任意 cwd 可跑。

---

## 2. 逐项结果

### 2.1 Spike 1：gevent + Flask SSE

- Content-Type 正确（`text/event-stream; charset=utf-8`）；token 有序完整（10/10）
- 首块 0.004s 到达 vs 总耗时 1.2s → 流式未被 monkey-patch 吞成一次性返回
- 出字节奏 ≈0.12s/token 保持

**结论**：现有 gunicorn 2x gevent 部署形态可以直接承载 SSE `/api/chat`，无需换 ASGI。

### 2.2 Spike 2：流式 tool_calls 分片聚合

mock 端点把一个 tool_call 拆成 8 个分片流式返回，验证聚合正确性：

- 聚合出恰好 1 个 tool_call：`name=query_liunian`、`args={'year': 2027}`、`id` 保留、无文本混入、`finish_reason=tool_calls` 传导
- ToolNode 执行**真实 `bazi_engine`**：2027 年干支实算为丁未，年干对日主甲木十神伤官——引擎数据经工具链路无损传导
- 第二轮返回纯文本无 tool_calls → 工具循环正确终止
- 全程 0.037s，gevent 下无死锁

**结论**：方案第 4 节"agent ⇄ tools 循环"核心机制成立。

### 2.3 Spike 3：SQLite WAL + 配额原子扣减

复刻 §2.1 的扣减设计 `UPDATE ... WHERE used < 5`：

- WAL 开启；10 并发进程抢 5 额度：**恰好 5 成功、used=5，无超卖无少卖**（单条 UPDATE 的原子性即防双花）
- 退款路径 `used-1` 正确回滚
- 对照组：无 busy_timeout 的并发进程确定性地报 `database is locked` → **busy_timeout=5000 是必要配置而非可选**
- 附带发现：gevent 单 OS 线程内 greenlet 因 sqlite C 调用不协程切换而天然串行化；真正的锁竞争来自多 worker 进程——spike 用"持锁事务 + 第二进程抢写"做确定性复现

**结论**：`chat_followups` 表 + gate 时原子扣 + finally 退款的设计在 WAL + busy_timeout 下成立。

### 2.4 Spike 4：双模型路线（DeepSeek + OpenAI GPT）

按用户补充"OpenAI 的 GPT 也可以用"验证协议互换性：

- **A 路线**：`ChatDeepSeek`（langchain-deepseek 1.1.0）→ mock 端点，流式 tool_calls 聚合 PASS
- **B 路线**：`ChatOpenAI`（langchain-openai 1.6.0）→ **同一** mock 端点，同样 PASS
- **C 可达性**：`api.deepseek.com` 经沙箱代理可达（401=连通待鉴权）；`api.openai.com` 沙箱出口受限（见 §5）
- D：产出版本锁定清单（§6）

**结论**：DeepSeek 与 OpenAI 是同一 OpenAI 兼容协议族，**切 GPT 只改 `model / api_base / api_key` 三个字段**，代码零改动。生产环境按用户确认 GPT 可用，作为降级/备选路线成立。

### 2.5 Spike 5：BGE 中文 embedding（RAG 轨道前置）

- `BAAI/bge-small-zh-v1.5`（fastembed/ONNX，无 torch）本地加载 0.2s（缓存后）
- 512 维向量正确
- 语义判别：两句命理文言相似度 **0.696** vs 命理/股市 **0.273** → 中文文言语义空间可用
- 单条查询 embedding **1.6ms**，远低于 R2 定的 50ms 查询侧门槛

**结论**：RAG 轨道的 embedding 底座成立，纯 CPU 部署无性能风险。模型分发注意走 HF 镜像或预打包进镜像。

### 2.6 Mini Agent：LangGraph 全链路（quickstart 验收件）

复刻 v4.1 目标图拓扑缩小版 `START → context_gate → agent ⇄ tools → END`，配 SqliteSaver：

- 首回合消息链 4 条（human+asst+tool+asst），system 不入历史
- context_gate 注入 digest 到独立 state 字段，**永不重复、永不乱序**
- 同 thread 第二回合累积到 8 条（checkpointer 记忆生效）
- **thread 隔离**：同账号换盘（t2）状态全新 4 条，不串扰 t1 的 8 条——串盘防线成立
- checkpoint 快照可读；`stream_mode="messages"` 流式出 token 正常

**结论**：目标架构的图机制、多轮记忆、会话隔离、流式全部就位。

---

## 3. 关键发现（写入 P1 实现注意清单）

以下 5 条都是 spike 实测踩到、文档/直觉容易错的点：

1. **`add_messages` reducer 只追加**：把 SystemMessage 塞进 state 会排在已有 human 之后。正确做法：digest 存独立 state 字段（`paipan_digest`），agent 节点调模型时**前置**进 prompt，不进会话历史。mini_agent 已按此实现并验证。
2. **langgraph 1.x 的 `recursion_limit` 是运行时 config 参数**（`graph.invoke(..., config={"recursion_limit": 25})`），不是 `compile()` 参数——与 0.x 教程写法不同。
3. **ToolNode 独立调用需显式传 `runtime=Runtime()`**：图内由框架注入，单测/脚本里直接 invoke 会报 `Missing required config key`。
4. **gevent ≠ 并发 sqlite**：单 OS 线程内 greenlet 在 sqlite C 调用上不切换、天然串行；锁竞争真实来源是多 worker 进程。故 WAL + `busy_timeout=5000` 必须配齐（spike 3 对照组已证）。
5. **mock 服务要同时支持 stream/非 stream 两种响应**：`invoke` 路径走 `stream=False`，只实现流式会让非流式验证假失败。

## 4. 对 v4.1 方案的反哺修订

| 方案条目 | spike 证据 | 修订动作 |
|---------|-----------|---------|
| §2.1 扣减时机 | spike 3 | 定案维持：gate 原子扣 + finally 退款，无需改 |
| §4 图拓扑 | mini_agent | 维持；digest 注入采用独立字段法（发现 1） |
| §4 模型层 | spike 4 | 维持 DeepSeek 主路线；OpenAI GPT 确认为零成本备选（3 字段切换） |
| §6 RAG 底座 | spike 5 | 维持 BGE-small-zh + fastembed；部署注意 HF 镜像/预打包 |
| §8 部署 | spike 1 | 维持 gunicorn+gevent，不迁 ASGI |
| requirements | spike 4/5 | P1 开工时合入 §6 版本锁定清单 |

## 5. 唯一未过项说明（OpenAI 域名连通）

- 现象：沙箱内 `api.openai.com` 经出口代理连接超时（`api.deepseek.com` 同路径 401 可达），且环境无 OpenAI 凭据。
- 判定：**沙箱出口策略限制，非工程问题**。用户确认生产环境 GPT 可用。
- 已做的等价验证：spike 4 B 路线让 `ChatOpenAI` 打 OpenAI 兼容端点，tool_calls 流式聚合完整通过——OpenAI SDK 协议栈在本方案代码路径上的行为已验证，差的只是网络与 key 两个环境变量。
- 残余风险：真实 GPT 模型的 tool_calls 分片行为（如 Unicode 转义差异）未实测。P1 若启用 GPT 路线，跑一次 `spike_4` 的 C 项即可闭环。

## 6. 版本锁定清单（P1 合入 requirements.txt）

```
langgraph==1.2.11
langgraph-checkpoint-sqlite==3.1.1
langchain-core==1.6.1
langchain-deepseek==1.1.0
langchain-openai==1.6.0
fastembed==0.8.0
onnxruntime==1.23.2
```

gevent / flask / requests / numpy 沿用现有 requirements。

## 7. P1 开工前置条件（已全部满足）

- [x] 图机制、工具循环、checkpointer、thread 隔离验证（mini_agent 9/9）
- [x] SSE 与 gevent 兼容（spike 1 4/4）
- [x] 配额原子扣/退设计验证（spike 3 5/5）
- [x] 模型双路线协议验证 + 版本锁定（spike 4）
- [x] RAG embedding 底座验证（spike 5 4/4）
- [x] spike 脚本落入仓库 `spikes/`，可移植复现（无需 PYTHONPATH）

P1 首个任务：`/api/chat` 骨架（context_gate + agent + query_liunian/query_bazi 工具 + SqliteSaver），SSE 输出接 spike 1 验证过的流式通道。
