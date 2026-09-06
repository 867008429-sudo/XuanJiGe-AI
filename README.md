# 玄机阁 · XuanJiGe AI

> 八字排盘 + DeepSeek 流式解读 + LangGraph 多轮追问的 AI 命盘咨询 Demo。

**在线体验**：[http://129.204.102.108:8888/](http://129.204.102.108:8888/)

当前项目仍处于内测阶段，命理解读仅作传统文化与自我观察参考，不作为医疗、投资、婚恋等现实决策依据。

![玄机阁首页](docs/screenshot-landing.png)

## 项目亮点

- **精确排盘**：基于 `sxtwl` 寿星天文历，按节气计算四柱、十神、五行、神煞与大运。
- **命盘咨询体验**：用户先建盘，再围绕同一命盘持续追问，不暴露工程后台和内部指标。
- **AI 流式解读**：DeepSeek V4 Flash 通过 SSE 逐段返回，报告链路和追问链路都支持流式体验。
- **Agent 追问链路**：LangGraph 编排只读工具，流年/大运/古籍检索由本地工具提供，模型负责表达和综合。
- **可信引用闭环**：古籍语料来自 `data/classics/`，追问引用使用可回验 `chunk_id`，并有离线抽审与流中兜底。
- **账号与额度**：注册登录、历史记录、缓存回放、每盘追问香火额度、helpful 反馈和超级测试账号机制已接入。
- **可部署**：SQLite + Docker Compose，一条命令即可在公网服务器运行。

## 仓库结构

```text
.
├─ app.py                    # Flask API + 内嵌单页前端
├─ bazi_engine.py            # 八字排盘核心算法
├─ ai_*.py                   # 报告解读：上下文、prompt、模型客户端、质量校验
├─ chat_*.py                 # 多轮追问：persona、工具、图编排、审计、清理
├─ classics_search.py        # 古籍本地检索 baseline
├─ data/classics/            # 古籍 JSONL 语料与版权说明
├─ tools/                    # 预检、备份、体验码、语料构建、抽审与评估脚本
├─ tests/                    # 单元测试与前端契约测试
├─ docs/                     # 技术长文、计划、进度、截图
├─ spikes/                   # Agent 技术验证实验
├─ Dockerfile
├─ docker-compose.yml
└─ requirements.txt
```

详细文档已收进 `docs/`，避免 GitHub 根目录过载：

- [文档索引](docs/README.md)
- [技术总览长文](docs/technical-overview.md)
- [Agent 化推进计划](docs/plans/agent-plan.md)
- [人格与香火体系计划](docs/plans/persona-plan.md)
- [项目进度日志](docs/progress/)
- [Agent spike 验证报告](spikes/README.md)

## 快速启动

### 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

然后访问 `http://127.0.0.1:8888/`。

### Docker 部署

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 等生产配置
docker compose up -d --build
curl http://127.0.0.1:8888/health
```

生产数据通过 Docker volumes 持久化，`.env` 和 `*.db` 已被 `.gitignore` 排除，勿提交密钥和数据库。

## 常用命令

```bash
# 全量测试
python -m unittest discover -s tests -p "test_*.py"

# 上线前预检
python tools/preflight.py

# 古籍检索评估
python tools/eval_classics_search.py --k 5

# 生成邀请体验码
python tools/manage_invite_codes.py generate 100

# 备份 SQLite
python tools/backup_sqlite.py
```

## API 概览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 服务健康检查 |
| `/api/paipan` | POST | 本地排盘，返回 `history_id` |
| `/api/interpret` | POST | AI 报告解读，SSE 流式返回 |
| `/api/chat` | POST | 命盘多轮追问，SSE 流式返回 |
| `/api/chat/quota` | GET | 查询某命盘追问余额 |
| `/api/chat/history` | GET | 恢复某命盘追问历史 |
| `/api/register` `/api/login` `/api/logout` | POST | 账号体系 |
| `/api/history` `/api/history/<id>` | GET/DELETE | 命盘历史记录 |
| `/api/stats` | GET | 管理统计，需配置 `ADMIN_TOKEN` |

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | 原生 HTML/CSS/JS，内嵌于 Flask 模板 |
| 后端 | Flask 3 + Gunicorn gevent |
| 排盘 | `sxtwl` 寿星天文历 |
| 模型 | DeepSeek V4 Flash |
| Agent | LangGraph + SQLite checkpointer |
| 数据 | SQLite |
| 部署 | Docker Compose |

## 后续方向

- P4 内测运营：体验码发放、真实追问样本、helpful 反馈和误导抽审率。
- RAG 增强：embedding / RRF / sqlite-vec 方案对比，用真实追问集做回归。
- 后台运营页：受保护地展示注册、成本、缓存、体验码和 helpful 数据。
- 域名与 HTTPS：当前公网地址可访问，正式对外建议绑定域名并启用证书。

## License

MIT
