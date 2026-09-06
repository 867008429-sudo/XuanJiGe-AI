# 玄机阁 Agent 化进度日志

## 2026-09-06

- 恢复上下文：读取 `agent-plan.md` 与 `PROGRESS-2026-09-06.md`，确认 P3 已完成到离线抽审与 token 机会性入账。
- 确认 R1 语料工程已生成 `data/classics/`：7 个 JSONL 文件 + `LICENSES.md`。
- 完成 R2 检索基线：新增 `classics_search.py`、`tools/build_classics_index.py`、`tools/eval_classics_search.py`、`tests/test_classics_search.py`。
- 定向验证：R1 corpus 测试 2 项通过；R2 search 测试 4 项通过；py_compile 通过；语料残留扫描 `bad=[]`；检索评估 top-5 20/20。
- 已同步 `README.md`、`agent-plan.md`、`PROGRESS-2026-09-06.md`，下一步为 R3 工具切换 + grounding。
- 完成 R3 baseline：`chat_tools` 切到 `search_classics`，`chat_graph` 增加 chunk_id 提取/校验，`/api/chat` 流中拦截未检索 chunk_id，前端增加 `search_classics` 工具提示。
- R3 定向验证：chat tools 19 项、chat graph 27 项、chat route 39 项、personas 15 项、frontend persona 5 项、M5 adversarial 6 项均通过；期间修复惰性导入导致的 NameError。
- 已同步 README、agent-plan、PROGRESS，下一步进入 R4 离线抽审引用真实性维度。
- 完成 R4 baseline：`chat_requests` 增加 `retrieved_chunk_ids_json`，`/api/chat` 保存本轮检索 ID，`chat_audit` 复用 grounding 校验并新增 `unverified_classic_chunk_id` 问题码。
- R4 对抗性审查：审计日志仍只存 issue code/hash；无引用正常通过、已检索 chunk_id 通过、伪造 chunk_id 失败；旧数据默认 `[]` 不会伪装成已验证出处。
- R4 定向验证：py_compile 通过；chat_db 28 项、chat_audit 9 项、chat_route 39 项、chat_graph 27 项、chat_tools 19 项、chat_cleanup 3 项均通过；PAT/secret 扫描无命中，diff check 仅行尾提示。
- 已同步 README、agent-plan、PROGRESS，下一步进入 P4 内测运营闭环。
- 完成 P4-1 helpful 采集：新增 `chat_feedback` 表、`/api/chat/feedback`、前端“有用/没用”按钮、后台 helpful 汇总，并接入删盘/retention 清理。
- P4-1 对抗性审查：只允许评价已完成回复；越权 hid 返回 404；反馈表不保存问题/回复/命盘/生辰；前端使用发送时锁定的 activeHid。
- P4-1 定向验证：chat_feedback 7 项、frontend persona contract 6 项、chat_route 39 项、chat_db 28 项、chat_cleanup 4 项、admin_stats 3 项通过。
- 收尾检查：GitHub token 常见前缀扫描无命中；`git diff --check` 无空白错误但有 Windows 行尾提示；`docker compose config` 因本机未安装 Docker CLI 未能运行。
- 本地服务已用当前代码启动在 `http://127.0.0.1:8888`，`/health` 返回 200；运行中的 exec session id 为 55511。
- 完成 P4-UI Agent Console 第一版：保留 Flask inline HTML/CSS/JS 与既有前端 ID，把页面改成左侧命盘输入/历史、中间 Agent 追问优先、右侧运行面板/引用可信度/内测指标。
- P4-UI 对抗性审查：chat 已在结果区内排到八字表前；`displayResult()` 会隐藏空状态；保留“每个命盘 10 炷香火、各道长耗香不同”文案；移动端横向溢出从 18px/7px 修到 0px。
- P4-UI 定向验证：`py_compile app.py tests/test_frontend_persona_contract.py` 通过；frontend persona contract 10 项通过；chat_feedback 7 项、chat_route 39 项通过；Playwright+本机 Chrome 检查 1440/900/375 宽度均无横向滚动。
- 本地服务已用最新 UI 代码重启在 `http://127.0.0.1:8888`，当前运行中的 exec session id 为 4702。

## 2026-09-07

- 按用户反馈完成 P4-UI 去后台化修订：`app.py` 首页从 Agent Console 改为“命盘咨询间”，保留左侧建盘/历史与中间继续追问，把原右侧工程面板改成底部用户说明卡片。
- P4-UI 对抗性审查：用户可见文案不再暴露 Agent Console、Grounding、P4、运行面板、内测指标、chunk_id、search_classics、checkpoint 或 issue code；相关能力改用“典籍参考、依据、使用边界”表达。
- 同步 README、agent-plan、task_plan、findings 与进度文件，避免后续实现继续沿用后台/Console 产品口径。
- P4-UI 定向验证：`py_compile app.py tests/test_frontend_persona_contract.py` 通过；frontend persona contract 11 项、chat_feedback 7 项、chat_route 39 项通过；`/health` 返回 200；PAT 常见前缀扫描无命中；`git diff --check` 仅有 Windows 行尾提示。
- P4-UI 视觉审查：Playwright 生成并目检 `artifacts/ui-user-consultation/` 桌面/手机截图，375px 无横向滚动，首屏与排盘后状态均是用户侧咨询体验。
- 完成本地测试超级账号支持：`accounts.is_superuser` 标记报告解读与 chat 香火均不扣额度；普通账号 JSON 契约保持兼容，超级账号额外返回 `unlimited: true`。
- 已在本地 `xuanjige.db` 创建/更新 `xjg_super` 超级测试账号（密码未写入仓库文件）；HTTP smoke 验证登录、`/api/quota`、`/api/paipan`、`/api/chat/quota` 与 persona affordable 均正常。
- 修复 P4 chat 空回复事故：实测确认 `ChatDeepSeek` 未显式关闭 thinking 时会先流出 `reasoning_content`，正文 `content` 可为空并打满输出预算，导致前端看不到追问回复。
- 代码修正：`chat_graph.resolve_model_kwargs()` 为 chat 模型传 `extra_body={'thinking': {'type': 'disabled'}}` 与 `stream_usage=True`；`parse_stream_event()` 支持文本 content blocks，并继续忽略 reasoning block / `reasoning_content`。
- 空回复兜底：`/api/chat` 在 `reply_text.strip()` 为空时记录 `empty_reply`、退还香火、不写 `chat_requests` 幂等缓存，并向前端返回错误事件；前端只对非空助手回复挂 helpful 按钮。
- 本地数据修复：删除 `acct:4:23` 下 2 条历史空 `chat_requests` 及对应 feedback，保留 usage logs 作为事故审计线索；当前本地空回复缓存数为 0。
- 对抗性审查：reasoning 不外发、工具 JSON 不外发、空回复不可评价/不可回放、普通用户退款、超级账号保持无限额度、旧空缓存不会再显示计香/反馈入口。
- 定向验证：`py_compile chat_graph.py app.py tests/test_chat_graph.py tests/test_chat_route.py tests/test_frontend_persona_contract.py` 通过；chat_graph/chat_route/chat_db/frontend/chat_feedback 共 118 项通过；真实掌门档 smoke 返回非空正文且 `reasoning_seen=False`；重启后本地 `/api/chat` 端到端返回 15 个 token 片段和 `done`。
- GitHub 发布：提交 `1319887 feat: build agent consultation loop` 已推送到 `origin/main`，远端 `refs/heads/main` 与本地 HEAD 一致。
- 上线前验证：显式测试入口 `python -m unittest discover -s tests -p "test_*.py"` 共 210 项通过；`tools/preflight.py` required checks 通过，只有 `ADMIN_TOKEN` 未配置的 optional warn；staged/仓库敏感信息扫描未发现 GitHub PAT 或 API key。
- 服务器部署尝试：公网 `129.204.102.108:8888` 与 22 端口可达，`/health` 正常，但当前本机没有 SSH 私钥/ssh-agent/SSH config；`ubuntu/root/lighthouse@129.204.102.108` 均返回 `Permission denied (publickey,password)`，远端页面尚未包含本次 `命盘咨询间`/空回复保护文案。
- 服务器部署完成：使用用户提供的临时 SSH 密码登录 `ubuntu@129.204.102.108`，保留旧目录 `/home/ubuntu/xuanjige` 与 `.env`，从 GitHub 克隆最新代码到 `/home/ubuntu/xuanjige_release`，并用 `docker compose -p xuanjige up -d --build` 复用既有 `xuanjige_*` volumes。
- 上线版本验证：服务器 release HEAD 为 `1319887cc4c11f8852361836b7485ceb0e572235`；容器 `xuanjige` `running healthy`、重启次数 0；公网 `/health`、`/api/auth-config` 均返回 200，首页已包含 `命盘咨询间`、空回复保护文案和超级账号文案。
- 服务器对抗性审查：容器内关键文件 `chat_graph.py/chat_audit.py/chat_cleanup.py/data/classics` 均存在；定向 `py_compile` 通过；运行时 checks 为 `ok`，`admin_stats` 仍因未配置 `ADMIN_TOKEN` 处于 optional disabled；最近日志无启动异常，只有外部扫描 `/admin/config.php` 的 404。
- 服务器超级账号：服务端创建/更新 `xjg_super_test` 超级测试账号；API smoke 验证登录 200、`/api/quota` 返回 `unlimited=true`、新建测试命盘后 `/api/chat/quota` 返回 `quota_left=999999999`，真实 `/api/chat` SSE 返回非空正文并 `done`，测试命盘已删除。
- GitHub 仓库整理：根目录保留代码、部署、测试与数据入口；长技术 README 保留到 `docs/technical-overview.md`，计划/进度移入 `docs/plans`、`docs/planning`、`docs/progress`，spike 报告移入 `spikes/README.md`。
- README 重写：新增干净项目首页，放入公网体验地址 `http://129.204.102.108:8888/`、项目亮点、仓库结构、快速启动、Docker 部署、常用命令、API 概览和文档索引。
- 对抗性审查：公网 Demo 地址是用户要求公开的 README 信息，因此从 `tools/preflight.py` 的敏感信息扫描中移除固定 IP 检查；PAT/API key/SSH 密码/超级账号密码仍继续扫描与禁止入库。
- 仓库整理验证：`py_compile` 通过；`.venv` 下 `tools/preflight.py` required checks 通过，仅 `ADMIN_TOKEN` 未配置为 optional warn；full unittest 210 项通过；敏感信息扫描无 GitHub PAT/API key/SSH 密码；`git diff --check` 仅 Windows 行尾提示。
