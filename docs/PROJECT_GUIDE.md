# 玄机阁项目接管手册

> 这份文档的目标不是只介绍“这个项目是什么”，而是帮你掌握它背后的专业知识：产品怎么拆、架构怎么想、AI 怎么调用、前后端怎么协作、服务器怎么部署、出问题怎么定位。你读完后，应该能自己判断下一步该改哪里、怎么改、怎么验收。

## 1. 项目一句话介绍

玄机阁是一个 AI First 的八字排盘与命理解读 Web 应用。

它的核心能力是：

1. 用户选择出生年月日时分和性别。
2. 后端用本地排盘引擎精确计算四柱、十神、五行、神煞、大运。
3. DeepSeek `deepseek-v4-flash` 基于结构化命盘生成中文长文本解读。
4. 前端用 SSE 流式展示 AI 输出，降低等待感。
5. 账号、配额、缓存、邀请码、注册限流共同控制 AI 成本和滥用。

你在面试时可以这样讲：

> 这是我独立用 AI 协作完成的一个小型全栈 Demo。它不是单纯套壳调用大模型，而是把确定性计算和生成式 AI 分开：排盘由本地算法完成，AI 只负责基于结构化结果做解释。项目包含 Flask API、SQLite 数据层、DeepSeek 流式调用、结构化生成、Docker 部署、注册风控、配额和缓存控制，已经部署到云服务器可在线体验。

## 2. 当前技术栈

| 层级 | 技术 | 作用 | 你需要理解的点 |
| --- | --- | --- | --- |
| 前端 | HTML/CSS/Vanilla JS | 单页应用界面、表单、弹窗、滚轮选择器、流式渲染 | DOM、事件、fetch、ReadableStream、响应式 CSS |
| Web 框架 | Flask | 提供页面和 API 路由 | 路由、请求/响应、JSON、Cookie、SSE |
| AI 服务 | DeepSeek Chat Completions | 结构化分析 + 最终流式解读 | prompt、temperature、max_tokens、stream、token 成本 |
| 排盘引擎 | Python + sxtwl | 天文历法、节气、四柱、大运计算 | 确定性计算不要交给 LLM |
| 数据库 | SQLite | 账号、会话、配额、缓存、历史记录、注册限流 | 表结构、索引、事务、查询 |
| 部署 | Docker + docker compose | 容器化运行服务 | 镜像、容器、volume、env_file、healthcheck |
| 生产服务 | Gunicorn + gevent | 生产 WSGI 服务，支持 SSE 长连接 | worker、timeout、并发、流式响应 |
| 运维 | Ubuntu 云服务器 | 运行 Docker 服务 | SSH、日志、端口、安全组、环境变量 |
| 版本管理 | Git + GitHub | 代码托管、展示作品 | commit、push、README、隐私保护 |

## 3. 项目文件地图

| 文件 | 你应该怎么理解 |
| --- | --- |
| `app.py` | 项目主入口。包含 Flask 路由，也内嵌了整页 HTML/CSS/JS。当前项目为了 Demo 快速上线，采用单文件前端交付。 |
| `ai_service.py` | 大模型调用核心。负责构建 prompt、两阶段结构化生成、DeepSeek 请求、SSE 流式解析、质量校验和成本估算。 |
| `bazi_engine.py` | 命盘计算核心。负责从出生时间和性别算出四柱、十神、五行、神煞、大运等结构化结果。 |
| `db.py` | 数据层。负责 SQLite 建表、账号、session、配额、历史、AI 缓存、注册尝试记录。 |
| `.env.example` | 环境变量模板。只放占位符和说明，不放真实 API Key、邀请码、密码。 |
| `Dockerfile` | 生产镜像定义。安装依赖，用 Gunicorn + gevent 启动 Flask。 |
| `docker-compose.yml` | 服务器部署入口。绑定端口、加载 `.env`、挂载数据库 volume 和核心代码文件。 |
| `README.md` | 给外部看项目亮点。更像作品展示页。 |
| `docs/PROJECT_GUIDE.md` | 你正在读的接管手册。更像内部学习和运维手册。 |

## 4. 核心架构

### 4.1 一张图理解

```text
用户浏览器
  |
  | 1. 填出生信息
  v
Flask /api/paipan
  |
  | 调用本地确定性算法
  v
bazi_engine.py
  |
  | 返回结构化命盘 JSON
  v
前端展示排盘结果
  |
  | 2. 用户点击 AI 解读
  v
Flask /api/interpret
  |
  | 检查登录、配额、缓存
  v
ai_service.py
  |
  | DeepSeek 两阶段生成
  v
SSE 流式返回给前端
```

### 4.2 最重要的架构原则

这个项目最重要的原则是：

> 确定性计算交给代码，非确定性表达交给大模型。

八字排盘本质上是历法和规则计算，模型容易算错。比如节气换月令、起运岁数、藏干十神，这些不应该让 LLM 现场推理。

所以当前架构是：

- `bazi_engine.py` 负责算准。
- `ai_service.py` 负责把结构化结果解释成人能读懂的语言。

这就是你可以在面试里强调的技术判断：**不是所有事情都应该交给大模型，LLM 应该放在它最擅长的生成和表达位置。**

## 5. 前端知识

### 5.1 为什么现在前端写在 `app.py` 里

这是一个 Demo 阶段的取舍。

优点：

- 部署简单，一个 Flask 服务就能跑完整页面。
- 不需要 Vite、Node、构建产物、静态资源托管。
- 改动快，适合快速验证产品。

缺点：

- 文件会越来越大。
- HTML/CSS/JS 混在一个 Python 字符串里，长期维护不舒服。
- 后续做复杂页面时，应该迁移到独立前端项目。

你可以这样判断是否需要迁移：

- 如果只有一个页面，继续单文件没问题。
- 如果要做个人中心、订单页、管理后台、邀请统计页，就应该拆成前后端分离。

### 5.2 当前前端负责什么

当前前端主要负责：

- 主题切换：深色/浅色。
- 出生时间选择：iOS 风格滚轮，支持鼠标拖动。
- 登录/注册弹窗：iOS 风格弹出和切换动画。
- 注册体验码字段：服务器开启邀请码后才显示。
- 排盘表单提交。
- AI 结果流式展示。
- AI 输出按【性格】【财运】【婚姻】【健康】【大运】【总评】切 Tab。
- 等待动画和状态提示。
- 历史记录展示。

### 5.3 SSE 流式渲染是什么

SSE 全称 Server-Sent Events，可以理解为：

> 后端不等 AI 一次性生成完，而是边生成边把文字推给前端。

用户体验上，SSE 的好处是：

- 用户马上看到“正在生成”，不会以为页面卡死。
- 长文本生成时等待感明显降低。
- 比 WebSocket 简单，适合单向推送。

当前前端使用的是 `fetch + ReadableStream`，不是浏览器原生 `EventSource`。原因是：

- `fetch` 更容易带上登录 token、client id 和 JSON body。
- 对 POST 请求更自然。

你要掌握的关键词：

- `response.body.getReader()`
- `TextDecoder`
- `data: {...}\n\n`
- 半截 buffer 处理

## 6. 后端 API 知识

### 6.1 主要 API

| API | 方法 | 作用 | 是否需要登录 |
| --- | --- | --- | --- |
| `/` | GET | 返回主页面 | 否 |
| `/health` | GET | 健康检查 | 否 |
| `/api/paipan` | POST | 本地排盘 | 否 |
| `/api/interpret` | POST | AI 解读，SSE 流式返回 | 是 |
| `/api/register` | POST | 注册账号，校验体验码和注册频率 | 否 |
| `/api/login` | POST | 登录 | 否 |
| `/api/logout` | POST | 退出登录 | 是 |
| `/api/me` | GET | 当前用户信息 | 否 |
| `/api/quota` | GET | 查询配额 | 否 |
| `/api/history` | GET | 查看历史 | 按当前指纹/账号隔离 |
| `/api/history/<id>` | GET/DELETE | 查看或删除单条历史 | 按当前指纹/账号隔离 |
| `/api/auth-config` | GET | 告诉前端是否需要体验码 | 否 |

### 6.2 为什么 `/api/paipan` 不要求登录

这是一个产品策略。

排盘是本地计算，成本几乎为 0，应该作为引流能力免费开放。AI 解读才消耗 API 成本，所以 AI 解读必须登录并受配额控制。

这是一种常见增长模型：

```text
低成本能力免费开放 -> 用户看到结果 -> 对深度解读产生兴趣 -> 登录领取免费额度
```

### 6.3 Cookie 和 Authorization

当前登录 token 支持两种读取方式：

1. `Authorization: Bearer <token>`
2. `auth_token` Cookie

这样做的好处是：

- 前端 fetch 可以主动带 token。
- 浏览器刷新后 Cookie 仍然能保持登录。

后续如果更正规，可以补：

- Cookie `secure=True`，但这要求 HTTPS。
- CSRF 防护。
- session 清理任务。

## 7. 数据库知识

### 7.1 当前 SQLite 表

| 表 | 用途 |
| --- | --- |
| `users` | 匿名用户配额，基于 fingerprint |
| `accounts` | 注册账号、密码 hash、免费次数、充值额度 |
| `sessions` | 登录 token 和账号绑定 |
| `history` | 用户排盘历史 |
| `ai_cache` | AI 解读缓存 |
| `usage_logs` | AI 调用成本和 token 记录 |
| `registration_attempts` | 注册尝试记录，用于限流和反滥用 |

### 7.2 SQLite 为什么适合当前阶段

SQLite 的特点：

- 单文件数据库。
- 不需要安装 MySQL/PostgreSQL。
- 备份就是复制一个文件。
- 对 Demo、小流量项目很适合。

什么时候应该换 PostgreSQL：

- 多台服务器同时写数据库。
- 并发用户明显增加。
- 需要复杂统计、后台管理、事务一致性更强。
- 要做正式付费系统。

### 7.3 缓存设计

AI 结果缓存的价值非常大。

原因：

- 同一个命盘重复解读，不应该重复扣费。
- 命理结果稳定，不需要每次都重新生成。
- 缓存命中可以做到 0 API 成本。

当前缓存 key 包含：

- prompt 版本
- 模型名称
- 是否 thinking
- 是否结构化生成
- 出生时间
- 性别
- 用户账号 fingerprint

为什么要包含 prompt 版本？

因为 prompt 升级后，旧缓存质量可能不够好。把版本放进 key，升级 prompt 后会自动走新生成，不会继续回放旧结果。

## 8. AI 调用知识

### 8.1 当前 AI 调用策略

当前使用 DeepSeek `deepseek-v4-flash`，适合中文长文本输出。

当前是两阶段生成：

```text
阶段 1：非流式 JSON 分析骨架
  |
  | response_format = json_object
  | temperature 较低
  v
校验六个板块是否完整、证据是否足够
  |
  v
阶段 2：基于骨架流式扩写正文
  |
  | stream = true
  v
前端逐字展示
```

### 8.2 为什么要结构化生成

直接让模型写长文，容易出现这些问题：

- 板块缺失。
- 顺序混乱。
- 财运、婚姻、健康写得空。
- 大运说错当前阶段。
- 古籍引用格式不统一。

结构化生成的意义是：

> 先让模型搭骨架，再让模型写文章。

这和人写文章一样：先列提纲，再写正文，质量会稳定很多。

### 8.3 Prompt 工程的核心

你要记住一个公式：

```text
好 Prompt = 角色 + 输入边界 + 分析方法 + 输出格式 + 禁止事项 + 质量标准
```

当前项目里的关键 prompt 约束包括：

- 模型不能重新排盘。
- 每个结论必须回扣盘面证据。
- 健康不能做疾病诊断。
- 财运不能做投资保证。
- 婚姻不能做绝对断语。
- 必须输出六个固定板块。
- 必须引用古籍义理。

这叫“把体感需求翻译成可执行约束”。

### 8.4 Temperature 怎么理解

`temperature` 控制模型输出的发散程度。

- 低 temperature：更稳定、更保守、更像说明书。
- 高 temperature：更有变化、更有文采，但更容易跑偏。

当前策略：

- 结构化骨架：`0.25`，要稳。
- 最终正文：默认 `0.7`，要有表达力。

### 8.5 Token 成本怎么理解

token 可以粗略理解为模型计费单位。

每次调用成本约等于：

```text
输入 token / 1,000,000 * 输入单价
+ 输出 token / 1,000,000 * 输出单价
```

控制成本的方法：

- 缓存命中不重新调用 AI。
- 限制 `max_tokens`。
- 不把无关数据塞进 prompt。
- 失败时不扣用户配额。
- 大输出用流式，减少用户中途关闭带来的体验损失。

## 9. 注册风控和邀请码

### 9.1 为什么要加体验码

没有邮箱、手机号、验证码时，注册门槛太低。用户可以无限注册账号，反复领取 5 次免费 AI 解读，直接消耗你的 API 成本。

体验码是 Demo 阶段最快的防滥用方案：

- 不需要短信供应商。
- 不需要邮箱发送服务。
- 不需要模板审核。
- 你可以手动发码给真实测试用户。

### 9.2 当前风控策略

服务器 `.env` 中配置：

```text
REGISTRATION_INVITE_CODES=code1,code2,code3
REGISTRATION_RATE_WINDOW_MINUTES=60
REGISTRATION_RATE_MAX_ATTEMPTS=8
REGISTRATION_DAILY_MAX_PER_IP=2
REGISTRATION_DAILY_MAX_PER_CLIENT=1
```

含义：

- 必须输入正确体验码才能注册。
- 1 小时内最多尝试 8 次注册。
- 同一 IP 每天最多注册 2 个账号。
- 同一设备每天最多注册 1 个账号。

### 9.3 注意：当前体验码还不是“一次性码”

现在的邀请码逻辑是：

> 只要码在 `.env` 列表里，就是有效码。

它没有记录“某个码是否已经被使用”。

这对 Demo 已经够用，但如果你要更正规，下一步应该把体验码做成数据库表：

```text
invite_codes
- code
- used_by_account_id
- used_at
- created_at
- disabled
```

这样每个码只能用一次，还可以统计谁用了哪个码。

## 10. 安全知识

### 10.1 当前已经做了什么

已经有：

- API Key 放 `.env`，不进 GitHub。
- 注册体验码不进仓库，只写服务器 `.env`。
- 登录 token 用随机 `secrets.token_hex`。
- 注册尝试限流。
- AI 解读必须登录。
- 配额失败不调用大模型。
- AI 失败不扣次数。
- 页面禁缓存，减少旧前端造成的错觉。

### 10.2 当前还不够正式的地方

这些是你后续要知道的专业风险：

1. 密码 hash 当前是 SHA256，正式产品应换成 Werkzeug/PBKDF2、bcrypt 或 Argon2。
2. 当前线上还是 IP + 8888 端口访问，不够专业，应该域名 + HTTPS + 反向代理。
3. Cookie 还没有 `Secure`，因为没有 HTTPS。
4. 没有 CSRF 防护。
5. 没有后台管理页。
6. 没有验证码、人机验证或邮箱/手机验证。
7. 邀请码不是一次性码。
8. 没有集中日志和异常告警。

### 10.3 最优先的安全升级顺序

推荐顺序：

1. 域名 + HTTPS + 反向代理。
2. 关闭公网直连 8888，只开放 80/443。
3. 密码 hash 升级。
4. 邀请码改成数据库一次性码。
5. 增加后台管理页。
6. 增加日志和告警。

## 11. 部署和运维知识

### 11.1 当前部署方式

服务器目录：

```text
/home/ubuntu/xuanjige
```

运行方式：

```bash
docker compose up -d --no-build --force-recreate
```

因为 `docker-compose.yml` 把核心文件 bind mount 进容器：

```yaml
volumes:
  - ./app.py:/app/app.py
  - ./bazi_engine.py:/app/bazi_engine.py
  - ./ai_service.py:/app/ai_service.py
  - ./db.py:/app/db.py
```

所以只改这些 Python 文件时，不一定需要重新 build 镜像，重启容器即可生效。

如果改了 `requirements.txt`，就必须重新 build。

### 11.2 常用运维命令

在服务器上：

```bash
cd /home/ubuntu/xuanjige
docker compose ps
docker logs -f xuanjige
docker inspect --format '{{.State.Health.Status}}' xuanjige
curl -fsS http://127.0.0.1:8888/health
```

重启：

```bash
docker compose up -d --no-build --force-recreate
```

如果依赖变了：

```bash
docker compose up -d --build
```

### 11.3 `.env` 是服务器命门

`.env` 里放的是运行时配置：

- DeepSeek API Key
- 模型名
- token 限制
- 结构化生成开关
- 注册体验码
- 注册限流参数
- 数据库路径

原则：

> `.env.example` 可以提交，`.env` 绝对不要提交。

### 11.4 如何判断线上是否真的更新了

更新后至少检查：

```bash
curl -fsS http://127.0.0.1:8888/health
curl -fsS http://129.204.102.108:8888/api/auth-config
```

如果 `/api/auth-config` 返回：

```json
{"invite_required": true}
```

说明服务器已经读到邀请码配置。

## 12. GitHub 和作品展示

### 12.1 好的 commit 应该怎么写

commit message 要说清楚“做了什么”，不要写“update”。

好例子：

```text
Add invite-based registration controls
Improve structured AI generation and UX polish
```

差例子：

```text
fix
update
111
```

### 12.2 面试官看这个项目会看什么

他大概率会看：

- 线上 Demo 是否能打开。
- README 是否写清楚架构。
- 有没有真实 AI 调用。
- 是否理解流式输出。
- 是否有成本控制。
- 是否有部署说明。
- 是否有安全意识。
- 代码是不是能跑。

你的亮点：

- 不是“只调用一个 API”，而是有本地排盘引擎。
- 有结构化生成和最终流式生成。
- 有账号、配额、缓存、注册风控。
- 有 Docker 部署。
- 有真实服务器在线。

## 13. 你后续怎么自己改功能

以后每次改功能，按这个流程走：

```text
1. 先描述用户问题
2. 找到对应代码位置
3. 小范围修改
4. 本地跑语法检查
5. 浏览器验证
6. 提交 GitHub
7. 部署服务器
8. 线上冒烟测试
```

本地检查命令：

```powershell
.\.venv\Scripts\python.exe -m py_compile app.py db.py ai_service.py bazi_engine.py
node -e "const fs=require('fs'); const src=fs.readFileSync('app.py','utf8'); const m=src.match(/INDEX_HTML = r'''([\s\S]*?)'''/); if(!m) throw new Error('INDEX_HTML not found'); const html=m[1]; [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].forEach((s,i)=>{new Function(s[1]); console.log('script '+(i+1)+' ok')});"
git diff --check
```

上线后检查：

```powershell
(Invoke-WebRequest -UseBasicParsing http://129.204.102.108:8888/health).Content
(Invoke-WebRequest -UseBasicParsing http://129.204.102.108:8888/api/auth-config).Content
```

## 14. 常见故障排查

### 14.1 页面打开但 AI 提示没配置 API Key

可能原因：

- 本地 `.env` 没有被加载。
- 服务器 `.env` 没有 `DEEPSEEK_API_KEY`。
- Docker 容器没有重启。
- 你访问的是旧端口或旧进程。

检查：

```bash
docker exec xuanjige python -c "import ai_service; print(bool(ai_service.DEEPSEEK_API_KEY)); print(ai_service.DEEPSEEK_MODEL)"
```

### 14.2 注册体验码不显示

可能原因：

- 服务器没有配置 `REGISTRATION_INVITE_CODES`。
- 容器没有重启。
- 浏览器缓存了旧页面。

检查：

```bash
curl -fsS http://127.0.0.1:8888/api/auth-config
```

应该返回：

```json
{"invite_required": true}
```

### 14.3 AI 输出很慢

可能原因：

- DeepSeek API 响应慢。
- 结构化生成第一阶段是非流式，要先等 JSON 骨架出来。
- 服务器网络慢。
- Nginx/Caddy 如果未来加反代，可能缓冲了 SSE。

关键知识：

> SSE 需要禁用代理缓冲，否则用户看不到逐字输出，只会最后一次性收到。

Nginx 里通常需要：

```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 300s;
```

### 14.4 Docker build 很慢

当前服务器拉 Python 包可能比较慢。

短期处理：

- 只改 `app.py/db.py/ai_service.py/bazi_engine.py` 时，用 `--no-build --force-recreate`。

长期处理：

- 配 pip 国内镜像。
- 保留 Docker layer cache。
- 不频繁改 `requirements.txt`。
- 用 CI/CD 在 GitHub Actions 里 build 镜像。

## 15. 下一步路线图

路线图不要只按“想做什么”排，而要按三个维度判断：

```text
用户价值：这个功能能不能让用户更愿意体验/留存/付费？
工程风险：这个改动会不会影响核心链路、数据安全、线上稳定性？
面试价值：这个功能能不能展示你具备全栈闭环、AI 调用、部署运维能力？
```

### 15.1 最值得做的 5 件事

1. 域名 + HTTPS：提升专业度和安全性。
2. README 再补一段“AI 调用逻辑和结构化生成流程”，方便面试官快速理解。
3. 邀请码改数据库一次性码，并做一个管理页。
4. 密码 hash 升级到 PBKDF2/bcrypt/Argon2。
5. 增加 AI 结果反馈按钮：有用/不准/重新生成。

### 15.2 按优先级拆解优化方向

| 优先级 | 优化项 | 为什么值得做 | 主要改哪里 | 验收方式 |
| --- | --- | --- | --- | --- |
| P0 | 域名 + HTTPS + 反向代理 | IP + 端口访问不专业，也不利于 Cookie Secure、安全展示和投递作品 | 服务器 Nginx/Caddy、DNS、安全组 | `https://域名` 可访问，HTTP 自动跳 HTTPS，8888 不公网暴露 |
| P0 | 密码 hash 升级 | SHA256 不适合存密码，正式项目至少用 PBKDF2/bcrypt/Argon2 | `db.py` 注册/登录逻辑 | 新账号用新 hash，旧账号可兼容登录或强制重置 |
| P0 | 邀请码一次性使用 | 当前码是“只要在 env 里就有效”，不能统计也不能防复用 | `db.py` 新增 `invite_codes` 表，`app.py /api/register` | 一个码注册成功后再次使用返回已使用 |
| P1 | 管理后台 | 你需要看到用户数、调用成本、邀请码使用、错误日志 | 新增 `/admin` 页面和管理员环境变量 | 能查看统计，能新增/禁用邀请码 |
| P1 | AI 反馈闭环 | 用户说“准/不准”是优化 prompt 和样本的关键数据 | 新增 `feedback` 表，前端结果页按钮 | 每条 AI 解读可记录 thumbs up/down 和原因 |
| P1 | Prompt A/B 版本 | 不同 prompt 效果要能对比，不靠体感拍脑袋 | `ai_service.py` prompt version 和 `usage_logs` | 同一命盘可标记 prompt 版本并比较反馈 |
| P1 | README 投递强化 | 面试官很少细读代码，README 要 30 秒内说清亮点 | `README.md` | 打开 GitHub 首屏能看到在线 Demo、架构、AI 调用、部署说明 |
| P2 | 前后端拆分 | 页面多起来后，内嵌 HTML 会难维护 | 新建 Vite/React 或 Nuxt 前端 | Flask 只做 API，前端独立构建部署 |
| P2 | 支付/充值 | AI 成本需要商业闭环，但接支付涉及合规和风控 | 订单表、支付回调、额度变更 | 支付成功后 credits 增加，失败不加 |
| P2 | 监控告警 | 线上 Demo 出问题要第一时间知道 | 日志、健康检查、错误通知 | API 异常和容器异常能通知到你 |

### 15.3 具体优化思路

#### 15.3.1 域名 + HTTPS

目标：

```text
http://129.204.102.108:8888
变成
https://你的域名
```

技术路径：

1. 购买域名。
2. 配 DNS A 记录指向服务器 IP。
3. 用 Caddy 或 Nginx 做反向代理。
4. 申请 HTTPS 证书。
5. 安全组只开放 `22/80/443`。
6. 关闭公网直接访问 `8888`。

你要掌握的知识点：

- DNS A 记录是什么。
- HTTP 和 HTTPS 区别。
- 反向代理是什么。
- TLS 证书为什么需要自动续期。
- SSE 经过 Nginx/Caddy 时为什么要关缓冲。

如果你用 Caddy，思路是：

```text
域名 {
    reverse_proxy 127.0.0.1:8888
}
```

Caddy 的好处是自动签发和续期 HTTPS，比 Nginx + Certbot 更适合 Demo 快速上线。

#### 15.3.2 邀请码改成数据库一次性码

当前状态：

```text
REGISTRATION_INVITE_CODES=code1,code2,code3
```

这是环境变量白名单，简单但不够精细。

更好的设计：

```sql
CREATE TABLE invite_codes (
    code TEXT PRIMARY KEY,
    used_by_account_id INTEGER,
    used_at TEXT,
    disabled INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

注册时逻辑：

```text
查 code 是否存在
查 disabled 是否为 0
查 used_by_account_id 是否为空
注册账号
把 code 绑定到新账号
```

为什么要放数据库：

- 可以做到一个码只用一次。
- 可以统计谁用了哪个码。
- 可以后续做后台生成/禁用码。
- 不用每次改 `.env` 后重启容器。

#### 15.3.3 AI 反馈闭环

现在 AI 输出质量主要靠你主观判断。下一步应该让用户反馈进入系统。

可以加三个按钮：

```text
准
不够准
重新生成
```

数据库表：

```sql
CREATE TABLE ai_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    rating TEXT NOT NULL,
    reason TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
```

产品价值：

- 你能知道哪类命盘最容易出问题。
- 可以收集“用户觉得不准”的真实样本。
- 后续优化 prompt 有依据。

技术价值：

- 展示你理解 AI 产品不是“一次生成完就结束”，而是需要反馈闭环。

#### 15.3.4 Prompt A/B 测试

不要只凭感觉说“新版 prompt 更好”。可以做 A/B。

思路：

```text
prompt_v5_structured：当前版本
prompt_v6_more_concrete：更强调具体事件和建议
prompt_v7_shorter_mobile：更适合移动端快速阅读
```

每次生成时记录：

- prompt version
- model
- temperature
- total tokens
- 用户反馈
- 是否缓存命中

然后你就能回答：

> 哪个 prompt 成本更低、反馈更好、生成更稳定？

这是很专业的 AI 产品思维。

#### 15.3.5 前后端拆分

现在单文件 `app.py` 很适合 Demo，但如果继续做完整产品，会遇到问题：

- CSS 越来越长。
- JS 状态越来越难维护。
- 多页面不好组织。
- 组件不能复用。

迁移方向：

```text
frontend/  React or Vue or Nuxt
backend/   Flask API
```

但不要太早拆。判断标准：

- 超过 3 个核心页面再拆。
- 需要复杂状态管理再拆。
- 要做 PWA/移动端体验再拆。

这叫“不要为了架构而架构”。

#### 15.3.6 从 SQLite 升级 PostgreSQL

当前 SQLite 很好，因为简单。但如果你要正式运营，会需要 PostgreSQL。

升级信号：

- 用户量明显增加。
- 需要后台复杂查询。
- 需要订单、支付、邀请码、反馈、日志多表联动。
- 需要更稳定的并发写入。

迁移思路：

1. 抽象数据库访问层，不要到处散落 SQL。
2. 用 SQLAlchemy 或保持手写 SQL 但统一封装。
3. 写一次迁移脚本，把 SQLite 数据导入 PostgreSQL。
4. 先在测试环境跑。

#### 15.3.7 观感和等待体验继续优化

你这个项目是玄学/占卜类产品，氛围很重要。后续可继续做：

- 结果生成时的“推演进度”更真实，比如“校验四柱”“定位用神”“分析当前大运”。
- AI 输出时每个 Tab 有骨架占位。
- 解读完成后出现“玄机阁印”或可分享海报。
- 移动端底部固定 CTA。
- 背景元素跟随滚动轻微视差，但要尊重 `prefers-reduced-motion`。

原则：

> 动效不是为了炫，而是为了让等待有反馈、让操作有连续性、让用户觉得系统正在认真推演。

### 15.4 如果要做成更完整产品

可以继续做：

- 用户中心。
- 充值/订单系统。
- 邀请码管理后台。
- AI 解读质量反馈。
- 每日运势。
- 合盘/事业/流年专题。
- 分享海报。
- 移动端 PWA。
- 域名、备案、HTTPS、监控告警。

## 16. 你应该补的专业知识清单

按优先级学：

1. HTTP 基础：GET/POST、Header、Cookie、状态码。
2. JavaScript fetch：JSON 请求、流式读取、错误处理。
3. Flask：路由、request、jsonify、Response、SSE。
4. SQLite：表、索引、查询、事务、备份。
5. Docker：镜像、容器、volume、compose、healthcheck。
6. Git：branch、commit、diff、push、ignore、secret hygiene。
7. Linux 运维：ssh、scp、日志、端口、system resources。
8. 大模型 API：messages、system/user、temperature、max_tokens、stream、usage。
9. Prompt 工程：角色、边界、证据、格式、反例、质量校验。
10. Web 安全：密码 hash、HTTPS、CSRF、XSS、限流、验证码。
11. 产品思维：免费能力、付费能力、成本控制、用户等待体验。
12. 面试表达：把“我做了什么”讲成“我为什么这样设计”。

### 16.1 知识点地图：概念、项目位置、练习方式

| 知识点 | 你要理解什么 | 项目里对应哪里 | 练习方式 |
| --- | --- | --- | --- |
| HTTP 状态码 | 200/400/401/403/429/500 分别代表什么 | `app.py` 各 API 返回 | 故意传错参数，看返回码和前端提示 |
| Cookie / Token | 登录态如何保存，为什么 token 要随机 | `get_auth_token`、`sessions` 表 | 登录后刷新页面，观察是否保持登录 |
| CORS | 浏览器为什么限制跨域请求 | `CORS(app)` | 了解未来前后端分离为什么会遇到跨域 |
| SSE | 后端如何不断推数据给前端 | `/api/interpret`、前端 `ReadableStream` | 断网/刷新/中途失败时看表现 |
| JSON Schema 思维 | 结构化输出为什么比纯文本稳定 | `build_structured_prompt`、`validate_structured_outline` | 给 AI 加一个新板块，补校验逻辑 |
| Prompt Version | 为什么 prompt 改了要进缓存 key | `PROMPT_VERSION`、`build_cache_key` | 改版本后同命盘重新生成 |
| 缓存 | 为什么缓存能省钱，什么时候不能缓存 | `ai_cache` 表 | 同一账号重复解读同一命盘，确认不扣次数 |
| 配额 | 免费次数和 credits 如何判断 | `check_quota_account` | 手动把免费次数用完，看返回 |
| 限流 | 为什么要限制注册尝试 | `registration_attempts` 表 | 连续输错体验码，观察 403/429 |
| 数据库索引 | 为什么 registration_attempts 要按时间和 IP 索引 | `idx_registration_attempts_ip_time` | 想象 10 万条注册记录时如何快速查询 |
| Docker Volume | 数据为什么不能存在容器临时层 | `db-data:/app/data` | 重启容器后历史记录还在 |
| Bind Mount | 为什么改 Python 文件后不一定 build 镜像 | `docker-compose.yml` volumes | 上传 `app.py` 后只重启容器 |
| Healthcheck | 为什么线上服务需要健康检查 | Dockerfile、compose healthcheck | 用 `docker inspect` 看 healthy |
| Secret Hygiene | 为什么 `.env` 不能提交 | `.env.example`、服务器 `.env` | `git status` 确认没有 `.env` |
| 反向代理 | 为什么未来要用 Nginx/Caddy | 未来 HTTPS 部署 | 画出 `浏览器 -> Caddy -> Flask` |

### 16.2 AI 应用必懂知识点

| 知识点 | 简单解释 | 本项目里的体现 |
| --- | --- | --- |
| System Prompt | 给模型设定身份、规则和边界 | 老道长角色、禁止重新排盘、六板块格式 |
| User Prompt | 给模型输入具体任务和数据 | 结构化命盘、当前年龄、当前大运标记 |
| Few-shot 思维 | 用示例约束模型风格 | JSON 示例结构、古籍引用格式 |
| Temperature | 控制稳定性和创造性 | 骨架低温，正文中等温度 |
| Max Tokens | 控制最大输出长度和成本 | `DEEPSEEK_MAX_TOKENS=6000` |
| Stream | 让用户边等边看 | DeepSeek stream + 前端逐字渲染 |
| Response Format | 要求模型输出 JSON | `response_format: json_object` |
| Validation | 不相信模型一定按格式输出 | `validate_structured_outline` |
| Fallback | AI 某一步失败时服务不能崩 | 结构化失败后走直接流式生成 |
| Cost Tracking | 每次调用要知道花了多少钱 | `calc_cost`、`usage_logs` |

你要形成的判断：

```text
如果任务需要稳定结构：先 JSON，再扩写。
如果任务需要用户等待：用 stream。
如果任务成本高：做缓存和配额。
如果模型容易幻觉：把事实由代码算好，只让模型解释。
如果模型输出要进 UI：必须有格式约束和校验。
```

### 16.3 Web 安全必懂知识点

| 风险 | 是什么 | 当前项目状态 | 后续做法 |
| --- | --- | --- | --- |
| 明文 HTTP | 数据传输可被中间人看到 | 当前 IP 访问仍是 HTTP | 上 HTTPS |
| 弱密码 hash | SHA256 太快，泄库后易被撞库 | 当前仍是 SHA256 | 换 PBKDF2/bcrypt/Argon2 |
| CSRF | 用户登录后被第三方页面诱导发请求 | 当前未做 | 重要写操作加 CSRF token |
| XSS | 用户输入被当 HTML 执行 | 前端多数用 textContent/escapeHtml | 保持不把用户输入直接 innerHTML |
| 暴力注册 | 无限注册薅免费次数 | 已有体验码 + 限流 | 一次性邀请码 + 验证码 |
| Secret 泄露 | API Key/Token/密码进 GitHub | 已避免 | 每次提交前扫敏感信息 |
| 端口暴露 | 直接暴露 8888 不专业 | 当前仍暴露 | 反代后只开放 80/443 |

### 16.4 后端工程知识点

你以后看后端代码，可以按这几个层次拆：

```text
路由层：接收请求、做参数校验、返回响应
业务层：判断登录、配额、缓存、风控
数据层：读写 SQLite
外部服务层：调用 DeepSeek
```

当前项目还没有完全分层，`app.py` 里承担了较多职责。这在 Demo 阶段可以接受，但长期可以拆成：

```text
routes/
  auth.py
  paipan.py
  interpret.py
services/
  quota_service.py
  invite_service.py
  ai_service.py
repositories/
  account_repo.py
  history_repo.py
  cache_repo.py
```

拆分原则：

- 不是为了“看起来高级”而拆。
- 当一个文件让你找不到逻辑时再拆。
- 先保证功能稳定，再谈架构优雅。

### 16.5 产品知识点

这个项目不是纯技术玩具，它有产品逻辑：

| 产品问题 | 当前答案 | 后续可优化 |
| --- | --- | --- |
| 用户为什么愿意开始用 | 排盘免费，门槛低 | 首页加示例结果和使用场景 |
| 用户为什么愿意登录 | 登录领 5 次 AI 解读 | 解释“登录后保存历史” |
| 用户为什么愿意等待 | 流式输出 + 推演动画 | 更细的进度阶段和结果骨架 |
| 用户为什么觉得专业 | 古籍引用、结构化板块、黑金道教风格 | 结果可分享、引用更可信 |
| 怎么控制成本 | 登录配额 + 缓存 + 限 token | 反馈低质量时允许免费重试一次 |
| 怎么防止滥用 | 邀请码 + 注册限流 | 一次性邀请码 + 验证码 + 风险 IP 黑名单 |

产品判断的核心是：

> 每个功能都要问：它解决了用户体验、成本、安全、转化里的哪一个问题？

### 16.6 你可以按这个顺序练手

1. 给 README 增加“在线 Demo + 技术亮点 + 部署说明”的更强首屏。
2. 把邀请码改成数据库一次性码。
3. 给 AI 结果加“准/不准/重新生成”反馈。
4. 给 `/api/stats` 做一个简单后台页面。
5. 把密码 hash 升级。
6. 配域名 + HTTPS。
7. 把前端从 `app.py` 拆到独立目录。
8. 给项目加一套最小自动化测试。

## 17. 面试讲法模板

你可以这样介绍这个项目：

> 我做了一个叫“玄机阁”的 AI 八字排盘 Demo。它的核心不是简单把用户输入丢给大模型，而是先用 Python 和 sxtwl 做确定性排盘，得到四柱、十神、五行、大运等结构化结果，再调用 DeepSeek `deepseek-v4-flash` 做两阶段生成：第一阶段输出 JSON 分析骨架，第二阶段基于骨架流式扩写为用户可读的六个板块。  
>  
> 技术上用了 Flask、SQLite、Docker、Gunicorn/gevent 和 SSE。为了控制成本，我做了账号配额、AI 解读缓存、token 成本记录；为了防止滥用，我加了注册体验码和 IP/设备限流。部署上已经跑在腾讯云 Ubuntu 服务器，通过 Docker Compose 管理服务。  
>  
> 这个项目让我完整走了一遍从产品想法、AI 调用、前后端实现、部署、日志排查、体验优化到安全风控的闭环。

## 18. 你真正要把控的不是代码，而是决策

你以后作为这个项目的负责人，最重要的不是会背每一行代码，而是能判断：

- 这个问题是前端体验问题，还是后端逻辑问题？
- 这个计算应该让代码做，还是让大模型做？
- 这个功能会不会增加 API 成本？
- 这个用户路径是不是太长？
- 这个数据应不应该进 GitHub？
- 这个改动上线后怎么验证？
- 这个问题如果线上发生，第一条日志该看哪里？

只要你能回答这些问题，你就不是“让 AI 帮你写代码的人”，而是“能指挥 AI 做完整产品的人”。
