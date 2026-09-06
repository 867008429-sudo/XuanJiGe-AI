# 玄机阁 Agent 化发现记录

## 2026-09-06

- R1 来源为 `isheng-eqi/bazi-ishengmind` 的 `skills/isheng-mind/data/`，源仓库 MIT；本地外部 checkout 位于 `.external/bazi-ishengmind`，不提交。
- 语料清洗只纳入古籍原文段落，显式剥离 `白话译文`、`关键词`、`现代启示` 和 luckclub 页面水印。
- luckclub 文件的章节编号可能写成 `第 1 0 章`，解析器需要兼容数字间空格。
- `滴天髓阐微.txt` 不走 luckclub 章节格式，需按 `通神论 一、天道` 等标题切分。
- R2 先做本地轻量检索 baseline，保持依赖克制；向量/embedding 可在 baseline 稳定后再接入。
- R2 评估中，过窄的期望书目会制造假失败：`父母兄弟六亲` 命中《三命通会》/《神峰通考》合理，`顺逆旺衰` 命中《滴天髓》原文也合理。
- 检索 smoke 显示《滴天髓原文》会因篇幅集中而抢占某些 top-1；R3 接入时应保留 top-k 多证据，不只让模型看 top-1。
- R3 grounding 的最小可行闭环是“工具结果 ID 白名单 + 流中 fake ID 保险丝”；不需要先引入数据库字段。
- app.py 对 chat_graph 保持惰性导入，新增 helper 不能直接引用模块全局，否则无 chat 依赖环境会退化为 NameError。
- R4 离线引用真实性需要保存“本轮检索到的 chunk_id 白名单”，仅存 ID JSON 即可；不需要复制工具输出摘录、用户问题、命盘 JSON 或出生信息。
- 旧回复没有检索白名单时应按“无法验证”处理；若正文含 chunk_id，离线抽审标记 `unverified_classic_chunk_id`，而不是默认放行。
- P4 helpful 采集的最小可用闭环是“已完成 request_id + 1/-1 评分”；不需要自由文本反馈，避免把新一类用户隐私引入数据库。
- docker-compose 热挂载必须跟随 RAG 新模块，否则重建前的容器叠加宿主 `chat_tools.py` 会找不到 `classics_search.py` 或 `data/classics`。
- P4-UI 第一版暴露运行面板、grounding、chunk_id、内测指标等工程词，会让 C 端用户误以为进了后台；用户侧只表达为“典籍参考、依据、使用边界”，工程概念留在受保护后台、README 技术段落和测试里。
- P4-UI 仍遵循“最大感知变化，最小技术改动”：保留 Flask inline 前端和既有 DOM/JS 契约，不在发码内测前切 React，避免把风险扩大到构建链路和路由迁移。
- ChatDeepSeek 接 DeepSeek V4 时若不显式传 `thinking: disabled`，流式事件会先出现 `additional_kwargs.reasoning_content`，正文 `content` 可能为空直到输出预算打满；追问链路应显式关闭 thinking，且继续禁止 reasoning 外发。
- `/api/chat` 的完成语义必须要求 `reply_text.strip()` 非空；空回复即使有 token usage 也只能记为 `empty_reply`、退款、不给幂等缓存和 helpful 入口。
