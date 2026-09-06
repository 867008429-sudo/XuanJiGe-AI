"""
玄机阁 - 对话图（LangGraph，agent-plan.md v4.1 §4）

图拓扑：START → context_gate → agent ⇄ tools → END

P0 spike 的三条结论性发现全部落在这里：
1. add_messages 只追加：digest 存独立 state 字段（paipan_digest），
   agent 节点调用模型时前置为 SystemMessage，不进会话历史 → 永不重复、永不乱序。
2. recursion_limit 是运行时 config 参数（非 compile 参数）。
3. 工具经闭包绑定命盘数据；额度原子扣与归属校验在路由层（gate）完成，
   图内不做任何 HTTP 语义的决策。
"""
import json
import os
import re
import sqlite3
import threading
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

import ai_service
from ai_context import build_bazi_context, context_to_prompt_text
from chat_personas import (
    DEFAULT_PERSONA_ID,
    build_persona_style,
    get_persona,
)
from chat_tools import build_chat_tools, format_name_reference

# ---------- 配置 ----------

CHAT_DB_PATH = os.environ.get(
    'CHAT_DB_PATH',
    os.path.join(os.path.dirname(__file__), 'data', 'chat.db'),
)
CHAT_MODEL_NAME = os.environ.get('CHAT_MODEL', '')
CHAT_API_BASE = os.environ.get('CHAT_API_BASE', '')
CHAT_MAX_TOKENS = int(os.environ.get('CHAT_MAX_TOKENS', '1500'))
CHAT_TEMPERATURE = float(os.environ.get('CHAT_TEMPERATURE', '0.7'))
CHAT_RECURSION_LIMIT = int(os.environ.get('CHAT_RECURSION_LIMIT', '25'))
CHAT_BUSY_TIMEOUT_MS = int(os.environ.get('CHAT_BUSY_TIMEOUT_MS', '5000'))
CLASSICS_CHUNK_ID_RE = re.compile(r'\b[a-z0-9]+(?:-[a-z0-9]+)*-ch\d{3}(?:-p\d{2})?\b')


class ChatConfigError(RuntimeError):
    """chat 链路配置缺失（如 API key），路由层应转 503。"""


def _resolve_model_name():
    return CHAT_MODEL_NAME or ai_service.DEEPSEEK_MODEL


def _resolve_api_base():
    if CHAT_API_BASE:
        return CHAT_API_BASE
    # 复用报告链路配置：完整 endpoint 去掉 /chat/completions 得 base
    url = ai_service.DEEPSEEK_API_URL or ''
    for suffix in ('/chat/completions', '/beta/chat/completions'):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url or 'https://api.deepseek.com'


def resolve_model_kwargs(persona=None):
    """persona 档位 → ChatDeepSeek 构造参数（纯函数，便于单测）。

    persona.model 非空串覆盖缺省模型名（掌门档 'deepseek-v4-pro'），
    空串回落 CHAT_MODEL 环境档；temperature / max_tokens 一并取人格配置。
    persona=None 走环境缺省档（兼容旧调用方）。
    """
    return {
        'model': (persona.model if persona and persona.model else _resolve_model_name()),
        'max_tokens': persona.max_tokens if persona else CHAT_MAX_TOKENS,
        'temperature': persona.temperature if persona else CHAT_TEMPERATURE,
        'extra_body': {'thinking': {'type': 'disabled'}},
        'stream_usage': True,
    }


def make_chat_model(persona=None):
    """构造 chat 模型客户端（DeepSeek 主路线，OpenAI 兼容协议）。

    模型档位随 persona（persona-plan §4.1，M3 接线）：每位道长绑定
    （模型名 + 采样参数 + 输出上限），对应通用 agent 产品的模型选择器。
    """
    if not ai_service.DEEPSEEK_API_KEY:
        raise ChatConfigError('DEEPSEEK_API_KEY 未配置，chat 链路不可用')
    from langchain_deepseek import ChatDeepSeek

    kwargs = resolve_model_kwargs(persona)
    return ChatDeepSeek(
        api_key=ai_service.DEEPSEEK_API_KEY,
        api_base=_resolve_api_base(),
        **kwargs,
    )


# ---------- checkpointer（进程级单例，WAL + busy_timeout） ----------

_checkpointer = None
_checkpointer_lock = threading.Lock()


def get_checkpointer():
    """惰性初始化的 SqliteSaver 单例。

    每个 gunicorn worker 进程持有一条长连接（gevent 单 OS 线程内
    greenlet 在 sqlite C 调用上不切换，天然串行，见 spike 3）。
    """
    global _checkpointer
    if _checkpointer is None:
        with _checkpointer_lock:
            if _checkpointer is None:
                os.makedirs(os.path.dirname(CHAT_DB_PATH) or '.', exist_ok=True)
                conn = sqlite3.connect(
                    CHAT_DB_PATH,
                    timeout=CHAT_BUSY_TIMEOUT_MS / 1000.0,
                    check_same_thread=False,
                )
                conn.execute(f'PRAGMA busy_timeout = {CHAT_BUSY_TIMEOUT_MS}')
                conn.execute('PRAGMA journal_mode = WAL')
                saver = SqliteSaver(conn)
                saver.setup()
                _checkpointer = saver
    return _checkpointer


def reset_checkpointer():
    """测试/热重置用：关闭并丢弃 SqliteSaver 单例。"""
    global _checkpointer
    saver = _checkpointer
    _checkpointer = None
    conn = getattr(saver, 'conn', None)
    if conn is not None:
        conn.close()


def delete_chat_thread(thread_id):
    """删盘级联的一部分：清掉该 thread 的全部 checkpoint（§5 P0）。"""
    get_checkpointer().delete_thread(thread_id)


# ---------- digest 与 system prompt ----------

def build_paipan_digest(paipan_data, name=''):
    """命盘摘要：报告链路的稳定 schema 直接复用（§3 资产复用清单）。"""
    context = build_bazi_context(paipan_data)
    digest = context_to_prompt_text(context)
    name_ref = format_name_reference(name)
    return f'{name_ref}\n{digest}'


def build_chat_contract():
    """chat 链路的行为契约（对应报告链路的 build_generation_contract，§4.6 主防线前置）。

    persona-plan §2.3：本函数的输出在全部人格间逐字一致——工具规则、
    回答规范、安全边界与通用口吻（先答所问、不用 markdown 标题），
    任何人格都不得改动一个字。人格化内容一律走 chat_personas。
    """
    return (
        '你在玄机阁接待已经看过命盘报告的用户，进行多轮追问对话。'
        '回答要具体、克制、有盘面依据。'
        '\n\n工具使用规则：'
        '1. 所有干支、十神、大运、流年数据必须通过工具获取（query_liunian / query_dayun），'
        '禁止自行推算历法或凭记忆报干支。'
        '2. 用户问到古籍义理或原文出处时先调用 search_classics；若返回"查无此文"，'
        '必须如实告知查无此文，禁止编造原文、卷页、作者或 chunk_id。'
        '3. 只有用户想临时看别人的盘时才用 query_paipan，并说明该结果不改变本会话命盘。'
        '4. 已有足够数据时直接回答，不为调工具而调工具。'
        '\n\n回答规范：'
        '1. 长度克制：简单追问 2-4 句，复杂问题最多 3 段；用户已看过大报告，不要复述全盘。'
        '2. 多轮连续性：回扣本轮对话已确认的事实（如刚查过的流年/大运），不重复查询已知数据。'
        '3. 每个判断回扣盘面证据（十神、五行、大运），不悬空断语。'
        '4. 禁止：投资收益保证、疾病诊断、婚姻绝对断语、恐吓式表达；健康话题只做养生级提醒。'
        '5. 古籍引用只可来自 search_classics 返回的 chunk；引用原文或篇名时必须附 chunk_id，'
        '格式如【ditiansui-chanwei-ch029】。'
        '\n\n口吻：先答用户所问，再给一句可执行的提醒；不用 markdown 标题。'
    )


def build_chat_system_prompt(persona=None):
    """人格化 system prompt = 契约区 + 风格区（persona-plan §2.3）。

    拼接顺序固定：契约在前、风格在后，使自缚条款的"上方规则"指代成立。
    persona=None 回落缺省人格（清虚道长，兼容不传 persona 的旧调用方）。
    """
    persona = persona or get_persona(DEFAULT_PERSONA_ID)
    return f'{build_chat_contract()}\n\n{build_persona_style(persona)}'


# ---------- State 与图 ----------

class ChatState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    paipan_digest: str        # 独立字段而非消息（spike 发现 1：system 排序问题）
    birth_info: dict          # 生辰+性别（gate 注入，schema 对齐 §4.1）
    chat_quota_left: int      # 剩余香火炷数（路由层每次 invoke 时更新）
    persona_id: str           # 本回合生效人格（记录用途；换人不锁死会话，§4.2）
    retrieved_chunks: list    # R3 Agentic RAG 轨道的 grounding 预留位


def build_chat_graph(paipan_data, name='', persona_id=DEFAULT_PERSONA_ID,
                     model=None, checkpointer=None):
    """为指定命盘编译对话图。

    - persona_id 经 chat_personas 白名单解析；未命中抛 ValueError
      （路由层已校验 400，走到这里说明是编程错误而非用户输入）。
    - model / checkpointer 可注入（测试打桩）；缺省走 make_chat_model(persona)，
      模型名与采样参数随人格档位（persona-plan §4.1，M3 接线）。
    - thread_id 由路由层用 db.build_chat_thread_id(fingerprint, hid) 服务端拼接。
    """
    persona = get_persona(persona_id)
    if persona is None:
        raise ValueError(f'未知人格: {persona_id!r}')
    tools = build_chat_tools(paipan_data)
    model = model or make_chat_model(persona)
    bound = model.bind_tools(tools)
    system_prompt = build_chat_system_prompt(persona)

    def context_gate(state: ChatState) -> dict:
        # digest 只在首回合注入；checkpointer 恢复的会话不再覆盖
        updates = {}
        if not state.get('paipan_digest'):
            updates['paipan_digest'] = build_paipan_digest(paipan_data, name)
        if not state.get('birth_info'):
            updates['birth_info'] = {
                'solar_date': (paipan_data or {}).get('solar_date', ''),
                'gender': (paipan_data or {}).get('gender', ''),
            }
        # 记录本次实际生效的人格（每回合更新为当前 prompt 所用人格；
        # 会话历史与工具事实跨人格共享，换人无信息损失，persona-plan §4.2）
        updates['persona_id'] = persona.persona_id
        return updates

    def agent(state: ChatState) -> dict:
        # digest 每次调用模型时前置进 prompt；不进会话历史 → 永不重复、永不乱序
        digest = state.get('paipan_digest') or ''
        msgs = [SystemMessage(content=f'{system_prompt}\n\n{digest}')] + state['messages']
        return {'messages': [bound.invoke(msgs)]}

    builder = StateGraph(ChatState)
    builder.add_node('context_gate', context_gate)
    builder.add_node('agent', agent)
    builder.add_node('tools', ToolNode(tools))
    builder.add_edge(START, 'context_gate')
    builder.add_edge('context_gate', 'agent')

    def route(state: ChatState) -> str:
        last = state['messages'][-1]
        return 'tools' if getattr(last, 'tool_calls', None) else END

    builder.add_conditional_edges('agent', route, ['tools', END])
    builder.add_edge('tools', 'agent')

    return builder.compile(checkpointer=checkpointer if checkpointer is not None else get_checkpointer())


def chat_invoke_config(thread_id):
    """运行时 config：thread_id 服务端拼接 + recursion_limit（spike 发现 2）。"""
    return {
        'configurable': {'thread_id': thread_id},
        'recursion_limit': CHAT_RECURSION_LIMIT,
    }


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get('type') or '').lower()
        if block_type in {'reasoning', 'reasoning_content', 'thinking'}:
            continue
        text = block.get('text', block.get('content', ''))
        if isinstance(text, dict):
            text = text.get('value', '')
        if isinstance(text, str):
            parts.append(text)
    return ''.join(parts)


def parse_stream_event(event):
    """把 stream_mode="messages" 的事件解析为可序列化的 SSE 载荷。

    只接受规范的 (message, metadata) 元组，其余一律跳过（防御未知事件格式）。
    token 只认 AI 消息的 content：ToolMessage（工具执行结果）与
    Human/System 回显绝不外发，否则工具的原始 JSON 会混进对话文本。
    返回 dict：{"type": "token"|"tool", ...}；无法识别的事件返回 None。
    """
    if not (isinstance(event, tuple) and len(event) == 2):
        return None
    msg, meta = event
    if getattr(msg, 'tool_call_chunks', None):
        chunks = msg.tool_call_chunks
        names = [
            str(c.get('name') or '')
            for c in chunks
            if isinstance(c, dict) and c.get('name')
        ]
        if names:
            return {'type': 'tool', 'name': names[0]}
        return None
    if isinstance(msg, (AIMessage, AIMessageChunk)):
        text = _content_text(getattr(msg, 'content', None))
        if text:
            return {'type': 'token', 'text': text}
    return None


def extract_chunk_ids(text):
    """Extract normalized RAG chunk ids from model or tool text."""
    return sorted(set(CLASSICS_CHUNK_ID_RE.findall(str(text or '').lower())))


def extract_retrieved_chunk_ids(event):
    """Collect chunk ids returned by the `search_classics` ToolMessage."""
    if not (isinstance(event, tuple) and len(event) == 2):
        return []
    msg, _meta = event
    if isinstance(msg, ToolMessage) and getattr(msg, 'name', '') == 'search_classics':
        return extract_chunk_ids(getattr(msg, 'content', ''))
    return []


def validate_grounded_citations(reply_text, retrieved_chunk_ids):
    """Validate that cited chunk ids are a subset of this turn's retrieved ids.

    This is the R3 grounding primitive. The streaming route can use it after a
    turn, and offline audit can reuse the same shape later for citation metrics.
    """
    cited = set(extract_chunk_ids(reply_text))
    retrieved = set(str(item).lower() for item in (retrieved_chunk_ids or []))
    missing = sorted(cited - retrieved)
    return {
        'ok': not missing,
        'cited_chunk_ids': sorted(cited),
        'retrieved_chunk_ids': sorted(retrieved),
        'missing_chunk_ids': missing,
    }


def dump_sse(payload):
    """SSE data 行（与现有 /api/interpret 的输出格式对齐）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
