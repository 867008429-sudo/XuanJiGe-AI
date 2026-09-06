# 玄机阁 Agent 化推进计划

## 目标

把现有报告型项目继续推进为可追问的咨询型 Agent，并按“完成一段、对抗性审查一段”的节奏落地。

## 当前阶段

| 阶段 | 状态 | 验收口径 |
|------|------|----------|
| P3 生产化补齐 | complete | 流中兜底、观测日志、清理、离线抽审、token 机会性入账均有定向测试 |
| R1 语料工程 | complete | 7 本古籍清洗为 JSONL；剥离译文/水印；LICENSES 声明来源与版权边界 |
| R2 检索基建 | complete | 本地检索模块 + 评估集；固定问题 top-5 命中率 100% |
| R3 工具切换 + grounding | complete | `search_classics` 替换字典版；跨 chunk 伪造 chunk_id 被流中拦截 |
| R4 抽审扩展 | complete | 离线抽审增加 `unverified_classic_chunk_id` 引用真实性维度 |
| P4 内测运营 | in_progress | helpful 采集与用户侧命盘咨询首屏已接入；剩余发码、真实追问样本与对外口径收口 |

## 下一步

1. P4-1 已完成：chat helpful 表、接口、前端按钮、后台统计、删盘/retention 清理均接入。
2. P4-UI 已完成用户侧修订：保留 Flask inline 前端与原有 ID/函数契约，把页面改成左侧建盘/历史、中间继续追问、底部咨询说明的命盘咨询间；不在用户可见界面暴露运行面板、grounding、chunk_id 或 P4 指标。
3. P4-chat 空回复事故已修复：chat 模型显式关闭 thinking、空回复退款不落库、前端不对空回复挂 helpful，且已清理本地 `acct:4:23` 的空回复缓存。
4. P4 代码已发布并上线到公网 `129.204.102.108:8888`：GitHub `origin/main` 与服务器 release 均为 `1319887cc4c11f8852361836b7485ceb0e572235`，服务器容器 healthy，真实 `/api/chat` SSE 非空。
5. GitHub 仓库整理完成：根目录瘦身，README 更新为干净项目首页并加入公网体验地址，内部计划/进度/长文档统一进入 `docs/`。
6. 进入 P4-2：用现有 `tools/manage_invite_codes.py` 生成 50-100 个体验码，保留“邀请制内测”口径。
7. 增加 P4 运营记录：真实追问会话数、helpful 反馈数、误导抽审率，避免简历/README 造数。
8. 仓库整理收尾验证已完成：`.venv` 下 full unittest 210 项通过，preflight required checks 通过，diff check 仅 Windows 行尾提示，PAT/API key/SSH 密码扫描无命中。

## 遇到的错误

| 错误 | 尝试次数 | 解决方案 |
|------|---------|---------|
| 在 PowerShell 中使用 Bash heredoc `python - <<'PY'` | 1 | 改用 PowerShell here-string `@'...'@ \| python -` |
| `/api/chat` grounding helper 访问不到惰性导入的 `chat_graph` | 1 | 保持惰性导入，把 `chat_graph.extract_chunk_ids` 作为参数传给 helper |
| `docker compose config` 无法运行 | 1 | 本机未安装 Docker CLI；已记录为待在 Docker 环境复验 |
| P4-UI 移动端排盘后横向溢出 18px/7px | 2 | 给 Agent 工作台子项加 `min-width:0/max-width:100%`，并重置 `.chat-send-btn` 的 `width:auto; flex:0 0 auto` |
| ChatDeepSeek 未显式关闭 thinking 导致 `/api/chat` 只收到 reasoning_content、正文为空 | 1 | 为 chat 模型传 `extra_body={'thinking': {'type': 'disabled'}}`，并给空回复加退款/不落库兜底 |
| PowerShell 管道前直接赋值环境变量失败 | 1 | 改成先单独设置 `$env:PYTHONIOENCODING='utf-8'`，再用 here-string 管道给 Python |
| 直接假设 SQLite 表含 `accounts.fingerprint` 或 `sessions.id` | 1 | 读取 schema 后改用 `acct:<account_id>` 与 `sessions.expires_at` |
| 服务器 Docker 构建从 PyPI 下载依赖过慢 | 1 | 保持构建不中断直到完成；后续可考虑给 Dockerfile 增加可配置 pip 镜像源/缓存策略 |
| 容器内完整 `tools/preflight.py` 执行挂住 | 1 | 中断只读预检，改用带超时的 `py_compile`、运行时 checks、HTTP 与日志检查 |
| 本机系统 Python 未安装 Flask，直接跑 `tools/preflight.py` 失败 | 1 | 改用项目 `.venv\Scripts\python.exe` 复跑，preflight required checks 通过 |
