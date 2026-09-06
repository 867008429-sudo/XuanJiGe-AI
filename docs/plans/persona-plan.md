# 玄机阁 人格层与香火计价方案 v1.0

> 定位：用户可自主选择不同风格的道长——对应 Trae 的"模型选择器"心智位。每位道长在卡片上标注"炷香"价格，把模型的成本分化透明交给用户选择。
>
> 前置依赖：P1 chat 链路已完成（`chat_tools.py` / `chat_graph.py` / `/api/chat` 路由，103 测试全绿）。本方案是 P2 的第一个模块，工程量约 4-5 天，不改图拓扑、不改工具层。
>
> 一条红线贯穿全案：**风格可变，事实不变**。不同道长对同一张盘的判断必须收敛，只允许表达分化——用户拿两位道长算出矛盾结论，信任即崩塌。

## 1. 产品设计

### 1.1 道长 = 配置捆绑包

与 Trae 模型选择器的对应关系：Trae 选的是（模型 + 参数）捆绑包，玄机阁的道长是（system prompt 人格 + 风格参数 + 模型档位）捆绑包。两者工程上是同一件事：一个配置维度同时穿透 prompt 层、模型层、计价层。

角色化产品的粘性数据已有验证：Character.AI 靠人格驱动做到 4500 万月活、人均每天 75 分钟，会话时长 25-45 分钟远超行业基准 12 分钟。命理用户带着情绪来，人格选择的情感投射比工具选择更强。

### 1.2 香火计价

"炷香"是唯一对用户可见的计价单位。模型、token、人民币都不出现在用户面前——用户看不懂 token，命理产品直接谈钱破坏氛围。

- 免费额度从"每盘 5 次追问"换算为"每盘 10 炷香"
- 1 炷锚定 flash 档模型 + 短回复（≤300 token）的均值成本
- 各道长定价 = ceil(该道长单次 token 成本 / 锚定成本)，向上取整，不允许出现亏本人格
- 上线前用评测集实测各人格 token 分布，校准定价后再开放

## 2. 人格注册表

### 2.1 数据结构

新文件 `chat_personas.py`，纯数据模块，无外部依赖：

```python
@dataclass(frozen=True)
class Persona:
    persona_id: str        # 白名单键，客户端可传，仅此一处用户可控
    title: str             # 道号，前端展示
    tagline: str           # 一句话人设，卡片副标题
    style_prompt: str      # 风格区 prompt（见 2.3 分区）
    temperature: float     # 人格化参数
    max_tokens: int        # 输出长度上限，按人格分档
    model: str             # '' = 复用 CHAT_MODEL；'deepseek-v4-pro' = 掌门档
    price_incense: int     # 炷香定价

PERSONAS: dict[str, Persona]    # 注册表本体
DEFAULT_PERSONA_ID = 'qingxu'   # 向后兼容：不传 persona 的旧客户端
```

注册表是白名单本身：`PERSONAS.get(persona_id)` 命中即合法，未命中一律 400。用户自定义人设（用户输入直接进 system prompt）是注入面，明确不做（见第 9 节）。

### 2.2 五位道长阵容

| persona_id | 道号 | 人设 | 模型档 | temperature | max_tokens | 定价 |
|---|---|---|---|---|---|---|
| `xuanzhen` | 玄真散人 | 江湖直白，结论先行，带点幽默 | flash | 0.8 | 800 | 1 炷 |
| `qingxu` | 清虚道长 | 严谨考据，引经据典（现有风格） | flash | 0.6 | 1200 | 2 炷 |
| `baiyun` | 白云师太 | 温和共情，委婉安抚 | flash | 0.7 | 1000 | 2 炷 |
| `tiekou` | 铁口神算 | 犀利断言，不留情面 | flash | 0.5 | 800 | 2 炷 |
| `zhangmen` | 掌门真人 | 深度推演，逐层论证 | pro | 0.6 | 2000 | 5 炷 |

`qingxu` 为缺省人格：其 style prompt 即现 `build_chat_system_prompt()` 的口吻段，不传 `persona` 字段的旧请求行为不变。掌门真人是订阅转化钩子（P4 起挂"掌门专享"角标，本期先按 5 炷开放内测）。

### 2.3 prompt 分区：契约区不变，风格区参数化

现 `build_chat_system_prompt()` 是整段字符串，按内容归属拆为两段：

- **契约区**：现"工具使用规则"与"回答规范"两段原样迁入；"口吻"段拆开——"先答用户所问，再给一句可执行的提醒"是通用行为，上提进契约区
- **风格区**：各人格的文风句式 + 人格化的口吻（"不寒暄、不客套"由各道长自行定义：玄真可以说俏皮话，白云允许软性开场）

```python
def build_chat_contract():
    """契约区：工具规则 + 回答规范 + 安全边界。全部人格逐字一致。"""

def build_persona_style(persona: Persona):
    """风格区：文风、口吻、开合句式。按人格注入。"""
```

拼接顺序固定为 `契约区 \n\n 风格区`，agent 节点现有的"system 前置不进历史"机制（spike 发现 1）原样复用。

每位道长的 style_prompt 开头都带同一句自缚条款：

> 你的个性不得凌驾于上方规则：涉及健康、投资、婚姻的边界，一律以规则区为准。

契约区在前、自缚条款置顶、对抗用例跨人格回归（见第 7 节）——三层防人设越界。

风格区草案（各 3-5 句，上线前过一轮人工试聊微调）：

- 玄真散人：先给结论再给一句理由；白话为主，术语出现即顺手解释；可以用一两句江湖口吻调剂，不贫嘴
- 清虚道长：每判断注明所据（十神/藏干/大运）；古籍义理注明出处书名；语速从容，不堆术语
- 白云师太：先承接情绪再回答；转折用"换个角度看"不用"但是你不行"；命理判断照常给，包装在宽慰里，不取消判断
- 铁口神算：结论一句话钉死；忌讳词汇直接点破；不用缓冲词；判断依据仍须给，只是不绕
- 掌门真人：分"盘面—推演—落点"三层展开；每层回扣证据；收尾给可执行的一句

白云师太条目里"不取消判断"是关键设计：共情人格最容易滑向"什么都好"的和稀泥，那也是事实失真。风格改变表达，不改变判断的锐度。

## 3. 香火计量模型

### 3.1 数据层改动

`chat_followups` 表结构不变，`used` 列语义从"条数"改"炷数"。三个函数全部参数化：

```python
def consume_chat_quota(hid, fingerprint, price, limit=None):
    """原子扣 price 炷。WHERE used + price <= limit 使检查+扣减仍是一条原子语句。
    返回 (是否成功, 扣减后已用炷数)。并发防双花语义不变。"""

def refund_chat_quota(hid, fingerprint, price):
    """失败退款：按实扣退 price 炷（唯一调用点在 /api/chat 异常分支）。"""

def get_chat_quota_left(hid, fingerprint, limit=None):  # 返回炷数，签名不变
```

`CHAT_FREE_FOLLOWUPS = 5` 改名 `CHAT_FREE_INCENSE = 10`，`.env.example` 同步。函数名沿用 `quota`：对代码而言它仍是"额度"，炷是单位；用户可见文案一律写"香火"。

`chat_requests` 幂等表加两列，记录本次实扣与人格：

```sql
ALTER TABLE chat_requests ADD COLUMN price INTEGER NOT NULL DEFAULT 2;
ALTER TABLE chat_requests ADD COLUMN persona TEXT NOT NULL DEFAULT 'qingxu';
```

`quota_left` 列名与语义（存写入时的剩余额度）不变，值从条数变炷数。

### 3.2 存量数据迁移

`init_db()` 内加幂等迁移：以 `chat_requests` 是否已有 `price` 列为迁移标记（`PRAGMA table_info` 检测），标记缺失才执行加列与两条 `UPDATE`，同事务提交。`chat_followups` 表结构不变，`used * 2` 若无标记裸跑会翻倍错账——列检测就是它的重入保护：

```sql
UPDATE chat_followups SET used = used * 2;   -- 每条原追问 ≈ 默认道长 2 炷
UPDATE chat_requests SET quota_left = quota_left * 2;
```

换算自洽：原一条追问的成本约等于现在默认人格一次 2 炷，`used=3` 条迁移成 6 炷、剩 4 炷，与迁移前"剩 2 次"的实际可用量等价。迁移只需跑一次（幂等检测保证重入安全）。

## 4. 图编排层改动

### 4.1 签名扩展

```python
def build_chat_graph(paipan_data, name='', persona_id=DEFAULT_PERSONA_ID,
                     model=None, checkpointer=None):
```

`make_chat_model(persona)` 按 `persona.model` 选模型名（空串回落 `CHAT_MODEL`），`temperature` / `max_tokens` 取人格配置。model 参数注入测试的打桩方式不变。

### 4.2 人格持久化与换道长

`ChatState` 加 `persona_id: str` 字段，`context_gate` 首回合注入。

中途换道长的语义定为**同 thread 换 system prompt，会话历史保留**。技术上是免费的：system prompt 本就每回合现拼（不进 messages 历史），新人格的 prompt 前置后，模型看到完整对话 + 新契约自然过渡。事实层（工具、排盘、规则）在人格间本就共享，换人无信息损失。

配套细节：

- 每次请求的 `persona` 即本次生效人格，checkpoint 里的 `persona_id` 只作记录与历史回放参考，不锁死会话
- 前端切换道长时给一行轻提示："已改请教 X 道长，前情他已看过"——不弹窗不打断
- 评测加"换人后人格一致性"用例（第 7 节）

### 4.3 幂等回放与换人

断线重发同 `request_id`：回放的是当时那位道长的回复，不扣费、不改写。用户在重试前换了道长，回放内容口吻与当前选择不一致——前端 `cached: true` 事件已提示"回放"，可接受；改写历史回复违背幂等表的语义约定（`save_chat_reply` 注释），不做。

## 5. 路由与 SSE 契约

### 5.1 新端点

```python
@app.route('/api/chat/personas', methods=['GET'])
def api_chat_personas():
    # ?hid=N 时按当前余额逐位标注 affordable，前端置灰余额不足的道长
    # 返回 [{persona_id, title, tagline, price, affordable, current}]
```

`/api/chat/quota` 路径与返回结构（`hid / quota_left / limit`）不变，值全为炷。

### 5.2 /api/chat 改动

请求体加 `persona`（可选，缺省 `qingxu`），校验时序插入在参数校验段之后、归属校验之前：

```
1. 登录 401 → 2. 参数 400（persona 不在白名单同此）→ 3. 归属 404
→ 4. 幂等回放（免费）→ 5. 编译图（persona 注入）→ 6. 原子扣 price 炷
→ 7. 流式生成；异常按实扣退款；成功落幂等表（含 price/persona）
```

扣费失败（403）响应体带明细，用户失败得明白：

```json
{"error": "香火不足", "message": "请教掌门真人需 5 炷香，当前余额 3 炷",
 "quota_left": 3, "persona_price": 5}
```

### 5.3 SSE 事件

`token` / `tool` / `error` 事件不变。`done` 扩展：

```json
{"type": "done", "quota_left": 4, "price": 2, "persona": "tiekou",
 "request_id": "req-001", "cached": false}
```

`X-Quota-Left` 响应头保留，值变炷数。回放路径的 `done` 带当时的 `price` 与 `persona`（从幂等表读）。

## 6. 前端改动

聊天区顶部加道长选择条：五位卡片横向排布，各含道号、一句 tagline、炷香角标；当前选中高亮，余额不足置灰（数据来自 `/api/chat/personas?hid=`）。切换即改后续请求的 `persona` 字段，附轻提示。

文案换算：`updateChatQuota` 保留函数名，显示改"香火 7 炷 / 香火已尽"；`done` 收尾追加一行"请教铁口神算，上香 2 炷"。403 分支读 `persona_price` 显示"该道长需 N 炷"。

新用户注册赠 1 炷"掌门体验香"：`chat_followups` 初值插入时 `used = -1`（余额 11）不可行——负数破坏 `used + price <= limit` 的原子语义。改为 `accounts` 侧记录一个 `zhangmen_trial_used` 标志，首次选掌门且未用过时仅扣 4 炷（`price - 1`）。此特性列为 P4，本期只留字段设计不实现。

## 7. 一致性评测

评测集从"质量闸门"升级为"人格一致性闸门"，是本方案的核心验收。

**跨人格事实收敛**：agent-plan §8 的 20 条单轮 golden set，断言层（应出现干支、十神、年份）与人格无关，5 人格 × 20 题 = 100 次跑批全过同一断言。事实要点分裂即红灯。

**规则区完整性对抗**：医疗诱导、投资保证类对抗用例 × 5 人格全过。铁口神算的犀利和白云师太的宽慰都不得突破安全边界——这两位是最可能越界的人格，对抗用例向他们倾斜。

**换人一致性**：同一 thread 内先用玄真问一轮、换白云追问第二轮，断言第二轮回扣第一轮事实且口吻切换。

**辨识度抽检**：抽 10 条双人格输出人工盲评"能否分辨是哪位道长"。判官模型评分（LLM-as-judge）列 P3 可选项，不进本期关键路径。

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 人格 prompt 越界（共情变和稀泥、犀利变恐吓） | 契约区置顶 + 风格区自缚条款 + 对抗用例跨人格回归 |
| 亏本人格烧穿免费额度 | 定价 ceil 向上取整 + max_tokens 按人格封顶 + 上线前实测校准 |
| 存量迁移错账 | `used * 2` 换算自洽（3.2）+ 迁移幂等 + 迁移前后抽 5 个账号人工核对 |
| 幂等回放与换人口吻错位 | `cached: true` 提示回放，不改写历史 |
| 风格 prompt 迭代引发漂移 | style_prompt 属版本化资产，改动必须重跑第 7 节评测 |

## 9. 明确不做的事

- 用户自定义道长人设——用户文本进 system prompt 即注入面，注册表白名单是唯一入口
- 道长语音、形象生成——人格先用纯文字立住
- 人格长期记忆（"道长认识你"）——P4 与订阅一起评估
- 香火充值通道——沿用"不做支付"红线，文案挂着
- 评测判官模型化——本期人工盲评，量小够用

## 10. 实施拆分

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 人格注册表 | `chat_personas.py` + prompt 分区拆分 + 单测 | 5 人格 prompt 拼接正确；契约区逐字一致断言过 |
| M2 香火计量 | db 参数化 + 列扩展 + 迁移 + 单测 | 并发扣减不双花（复用 spike 3 模式）；退款按实扣；迁移幂等 |
| M3 路由与模型档 | persona 参数 + 403 明细 + SSE 扩展 + `/api/chat/personas` | 路由测试全过；旧客户端（不传 persona）行为不变 |
| M4 前端 | 选择条 + 香火文案 + 换人提示 | 手工过一遍五人格切换与余额不足置灰 |
| M5 一致性评测 | 第 7 节全套 | 100 次跑批事实收敛；对抗全过；换人用例过 |

总计约 4-5 天。M1-M2 纯后端可先行合入，M3 起前后端契约变更同一提交（沿用 README 与代码同 PR 的纪律）。
